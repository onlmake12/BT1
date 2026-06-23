### Title
Verify Queue Lacks Fee-Rate Eviction, Enabling Cheap DoS of Transaction Processing Pipeline — (File: `tx-pool/src/component/verify_queue.rs`)

---

### Summary

The `VerifyQueue` in CKB's tx-pool enforces a hard 256 MB total-size cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`) with **no fee-rate-based eviction**. An unprivileged user can flood the queue with minimum-fee transactions, causing every subsequent `add_tx` call to return `Reject::Full` — blocking all transaction processing for every other user. Unlike the main pool, which evicts the lowest-fee-rate entries when it overflows, the verify queue simply refuses new entries once full, regardless of how high their fee rate is.

---

### Finding Description

`VerifyQueue` in `tx-pool/src/component/verify_queue.rs` tracks a running `total_tx_size` counter. The admission gate is:

```rust
// line 104-106
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```

When `is_full` returns `true`, `add_tx` immediately returns an error:

```rust
// lines 215-219
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
``` [1](#0-0) [2](#0-1) [3](#0-2) 

The entire `VerifyQueue` API exposes only `remove_tx`, `remove_txs`, `remove_txs_by_peer`, `pop_front`, and `clear`. There is **no** `limit_size` or fee-rate-ordered eviction path — a stark contrast to the main pool:

```rust
// tx-pool/src/pool.rs lines 292-328
pub(crate) fn limit_size(...) {
    while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
        // evict lowest-fee-rate entry
    }
}
``` [4](#0-3) 

Both `submit_remote_tx` (P2P relay path) and `notify_tx` (miner proposal path) funnel through `resumeble_process_tx`, which calls `add_tx` on the shared `verify_queue`:

```rust
// tx-pool/src/process.rs lines 355-384
async fn resumeble_process_tx_and_notify_full_reject(...) -> Result<bool, Reject> {
    ...
    let ret = self.resumeble_process_tx(tx, is_proposal_tx, remote).await;
    if matches!(ret, Err(Reject::Full(_))) {
        self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
    }
    ret
}
pub(crate) async fn submit_remote_tx(...) -> Result<bool, Reject> {
    self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer))).await
}
pub(crate) async fn notify_tx(...) -> Result<bool, Reject> {
    self.resumeble_process_tx_and_notify_full_reject(tx, true, None).await
}
``` [5](#0-4) 

An attacker who fills the 256 MB verify queue causes every subsequent call to `add_tx` — from any peer or local RPC caller — to return `Reject::Full`, halting the entire pre-verification pipeline.

The unit tests confirm this behaviour explicitly:

```rust
// tx-pool/src/component/tests/chunk.rs lines 376-401
service.verify_queue.write().await
    .set_total_tx_size_for_test(256_000_000 - 1);
let ret = service.submit_remote_tx(tx, MAX_TX_VERIFY_CYCLES, 1.into()).await;
assert!(matches!(ret, Err(crate::error::Reject::Full(_))));
``` [6](#0-5) 

---

### Impact Explanation

Once the verify queue is saturated:

- **`submit_remote_tx`** returns `Reject::Full` for every relayed transaction from every peer — relay of legitimate transactions is halted.
- **`notify_tx`** returns `Reject::Full` for every miner proposal transaction — miners cannot get proposal transactions into the pipeline.
- The main pool's fee-rate eviction is never reached because transactions never leave the verify queue stage.

The attacker's transactions remain in the queue until workers drain them. If the attacker uses declared cycles above `max_tx_verify_cycles` (marking them `is_large_cycle = true`), they are routed to a slower worker path, extending the window of denial. [7](#0-6) 

---

### Likelihood Explanation

- **Entry path is fully unprivileged**: any RPC caller (`send_transaction`) or P2P peer can submit transactions.
- **Cost is low**: `TRANSACTION_SIZE_LIMIT` is 512 KB; filling 256 MB requires ~512 transactions. At the default minimum fee rate of 1 000 shannons/KB, each 512 KB transaction costs ~512 000 shannons (≈ 0.005 CKB). Total attack cost ≈ 2–3 CKB.
- **No eviction race**: unlike the main pool, the verify queue never evicts by fee rate, so a higher-fee legitimate transaction cannot displace the attacker's entries.
- **Sustained attack**: the attacker can continuously resubmit as workers drain entries, keeping the queue perpetually full. [8](#0-7) [9](#0-8) 

---

### Recommendation

**Short term**: When `add_tx` finds the queue full, compare the incoming transaction's fee rate against the lowest-fee-rate entry already in the queue. If the incoming fee rate is higher, evict the lowest entry and admit the new one (analogous to the main pool's `limit_size`).

**Long term**: Expose a `min_verify_queue_fee_rate` floor that rises dynamically as the queue fills, mirroring the main pool's dynamic minimum fee rate. This ensures the verify queue cannot be cheaply saturated by minimum-fee spam.

---

### Proof of Concept

1. Attacker holds enough CKB to create ~512 transactions, each consuming ~512 KB of serialized size.
2. Attacker sets each transaction's fee to exactly the `min_fee_rate` threshold (1 000 shannons/KB) and declares cycles as `max_tx_verify_cycles + 1` to route them to the slow large-cycle worker.
3. Attacker submits all transactions via `send_transaction` RPC (or P2P relay).
4. After ~512 submissions, `verify_queue.total_tx_size` reaches 256 MB.
5. Every subsequent `send_transaction` call from any user returns `PoolIsFull (-1106)`.
6. Miners calling `notify_tx` for proposal transactions also receive `Reject::Full`, stalling block assembly.
7. Attacker resubmits new transactions as workers drain old ones, sustaining the DoS indefinitely at minimal cost. [10](#0-9) [11](#0-10)

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

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
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

**File:** tx-pool/src/pool.rs (L292-328)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
```

**File:** tx-pool/src/process.rs (L355-384)
```rust
    async fn resumeble_process_tx_and_notify_full_reject(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let tx_hash = tx.hash();
        let ret = self.resumeble_process_tx(tx, is_proposal_tx, remote).await;

        if matches!(ret, Err(Reject::Full(_))) {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }

        ret
    }

    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }

    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
    }
```

**File:** tx-pool/src/component/tests/chunk.rs (L376-402)
```rust
#[tokio::test]
async fn submit_remote_tx_notifies_relayer_when_verify_queue_is_full() {
    let (service, tx_relay_receiver) = service_with_relay_receiver();
    let tx = build_tx(vec![(&H256([1; 32]).into(), 0)], 1);
    let tx_hash = tx.hash();

    service
        .verify_queue
        .write()
        .await
        .set_total_tx_size_for_test(256_000_000 - 1);

    let ret = service
        .submit_remote_tx(tx, MAX_TX_VERIFY_CYCLES, 1.into())
        .await;

    assert!(matches!(ret, Err(crate::error::Reject::Full(_))));
    match tx_relay_receiver
        .try_recv()
        .expect("expected reject notification")
    {
        TxVerificationResult::Reject { tx_hash: rejected } => {
            assert_eq!(rejected, tx_hash);
        }
        _ => panic!("expected reject notification"),
    }
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-20)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```
