### Title
Unbounded Per-Peer Submissions to `VerifyQueue` Enable FIFO-Based Processing Delay for Legitimate Transactions — (File: `tx-pool/src/component/verify_queue.rs`)

---

### Summary

The `VerifyQueue` in CKB's tx-pool processes incoming transactions in FIFO order (by `added_time`) with only a global 256 MB size cap and no per-peer or per-sender submission limit. An unprivileged attacker — reachable via the RPC `send_transaction` endpoint or the P2P relay protocol — can flood the queue with many minimum-fee transactions, causing legitimate users' transactions to be delayed behind the attacker's entries or rejected outright when the queue is full. This is the direct CKB analog of the EigenLayer withdrawal-queue griefing attack.

---

### Finding Description

`VerifyQueue` is the staging area where all incoming transactions wait before being picked up by verify workers. It is sorted by `added_time` (wall-clock milliseconds at insertion) and drained FIFO by `pop_front`.

**Root cause — no per-peer cap in `add_tx`:**

```rust
// tx-pool/src/component/verify_queue.rs
pub fn add_tx(
    &mut self,
    tx: TransactionView,
    is_proposal_tx: bool,
    remote: Option<(Cycle, PeerIndex)>,
) -> Result<bool, Reject> {
    ...
    if self.is_full(tx_size) {          // only global 256 MB check
        return Err(Reject::Full(...));
    }
    ...
    self.inner.insert(VerifyEntry {
        added_time: unix_time_as_millis(),   // FIFO key
        ...
    });
``` [1](#0-0) [2](#0-1) 

The only admission guard is the global `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` bytes: [3](#0-2) 

There is no per-peer counter, no per-sender quota, and no minimum-value floor enforced at the queue level.

**FIFO drain by workers:**

Workers call `pop_front(only_small_cycle)`, which calls `peek` → `iter_by_added_time().next()` — strictly oldest-first: [4](#0-3) 

Workers in `verify_mgr.rs` loop on `pop_front` until the queue is empty: [5](#0-4) 

**Attacker entry paths:**

1. **RPC `send_transaction`** → `resumeble_process_tx` → `enqueue_verify_queue` → `VerifyQueue::add_tx`. No rate limit exists on the RPC path. [6](#0-5) 

2. **P2P relay `RelayTransactions`** → `TransactionsProcess` → same path. The relay rate limiter is 30 messages/second per `(PeerIndex, message_type)` pair, but each message may carry up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` transactions: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

1. **Queue saturation / rejection of legitimate transactions.** Once the attacker's transactions occupy the 256 MB verify queue, any subsequent `send_transaction` RPC call or relayed transaction from a legitimate user is rejected with `Reject::Full`. The user receives an error and must retry later.

2. **FIFO processing delay.** Even before the queue is full, the attacker's transactions — inserted earlier — are always dequeued before the legitimate user's transactions. Workers process the attacker's backlog first, introducing unbounded latency for honest users proportional to the attacker's queue depth.

3. **Continuous replenishment.** As workers drain the attacker's entries, the attacker can immediately resubmit new transactions (using the same or different UTXOs), keeping the queue perpetually saturated. This mirrors the EigenLayer scenario where the attacker front-runs `processWithdrawal` to re-fill the queue.

4. **Asymmetric cost.** Verification workers bear the CPU cost of processing attacker transactions. The attacker pays only the minimum fee rate (1000 shannons/KB by default) and on-chain transaction fees, while imposing sustained verification load and queue occupancy on the node.

---

### Likelihood Explanation

- **Entry path is fully unprivileged.** Any RPC caller or P2P peer can submit transactions without any special role.
- **Economic cost is real but low.** The attacker needs valid UTXOs and pays `min_fee_rate`. With a modest UTXO set (e.g., 10,000 cells each holding the minimum capacity), an attacker can keep thousands of transactions in the queue continuously.
- **No per-peer or per-sender guard exists** in the verify queue, unlike the orphan pool (`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`) or the unknown-tx-hash tracker (`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`), which do have per-peer caps. [9](#0-8) [10](#0-9) 

---

### Recommendation

1. **Add a per-peer submission cap to `VerifyQueue`.** Track how many bytes (or entries) each `PeerIndex` currently occupies in the queue. Reject `add_tx` when a single peer exceeds a configurable fraction of `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`.

2. **Add RPC-path rate limiting.** Apply a per-IP or per-connection rate limit to `send_transaction` analogous to the relay rate limiter already present in `Relayer::try_process`.

3. **Consider fair-queuing instead of strict FIFO.** A weighted-fair or round-robin drain across peers would prevent any single peer from monopolizing worker time even if they fill their per-peer quota.

---

### Proof of Concept

**Attacker setup:** Attacker controls 50,000 UTXOs on mainnet, each holding the minimum CKB capacity.

**Step 1 — Flood via RPC (no rate limit):**
```
for utxo in attacker_utxos:
    send_transaction(build_min_fee_tx(utxo))   # RPC, no throttle
```
Each transaction is ~200 bytes serialized. 50,000 × 200 B = 10 MB — well within the 256 MB cap, leaving room to repeat.

**Step 2 — Queue is now occupied by attacker entries, all with `added_time` earlier than any honest user's submission.**

**Step 3 — Honest user calls `send_transaction`.** Their transaction enters the queue behind all attacker entries. Workers drain attacker entries first (FIFO by `added_time`).

**Step 4 — Attacker replenishes.** As workers clear attacker entries, the attacker submits the next batch, keeping the queue perpetually ahead of honest users.

**Result:** Honest users experience indefinite verification delay or `Reject::Full` errors, unable to get their transactions into the pending pool and eventually into a block — a direct analog to the EigenLayer withdrawal-queue DoS. [11](#0-10) [12](#0-11)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L56-65)
```rust
pub(crate) struct VerifyQueue {
    /// inner tx entry
    inner: MultiIndexVerifyEntryMap,
    /// subscribe this notify to get be notified when there is item in the queue
    ready_rx: Arc<Notify>,
    /// total tx size in the queue, will reject new transaction if exceed the limit
    total_tx_size: usize,
    /// large cycle threshold, from `pool_config.max_tx_verify_cycles`
    large_cycle_threshold: u64,
}
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L170-194)
```rust
    /// Returns the first entry in the queue and remove it
    pub fn pop_front(&mut self, only_small_cycle: bool) -> Option<Entry> {
        if let Some(short_id) = self.peek(only_small_cycle) {
            self.remove_tx(&short_id)
        } else {
            None
        }
    }

    /// Returns the first entry in the queue
    pub fn peek(&self, only_small_cycle: bool) -> Option<ProposalShortId> {
        let mut iter = self.inner.iter_by_added_time();

        if let Some(proposal_entry) = iter.find(|e| e.is_proposal_tx) {
            return Some(proposal_entry.inner.tx.proposal_short_id());
        }

        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };

        entry.map(|e| e.inner.tx.proposal_short_id())
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-237)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
    }
```

**File:** tx-pool/src/verify_mgr.rs (L109-163)
```rust
    async fn process_inner(&mut self) {
        loop {
            if self.exit_signal.is_cancelled() {
                info!("Verify worker::process_inner exit_signal is cancelled");
                return;
            }
            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }
            // cheap query to check queue is not empty
            if self.tasks.read().await.is_empty() {
                return;
            }

            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }

            // pick a entry to run verify
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
                    Some(entry) => entry,
                    None => {
                        if !tasks.is_empty() {
                            tasks.re_notify();
                            debug!(
                                "Worker (role: {:?}) didn't got tx after pop_front, but tasks is not empty, notify other Workers now",
                                self.role
                            );
                        }
                        return;
                    }
                }
            };

            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
    }
```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** sync/src/relayer/mod.rs (L88-99)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```
