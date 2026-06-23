### Title
VerifyQueue Lacks Per-Peer Fairness Scheduling, Enabling Single-Peer DoS of Transaction Verification Pipeline — (File: `tx-pool/src/component/verify_queue.rs`)

---

### Summary

The `VerifyQueue` in CKB's tx-pool processes transactions in strict FIFO order by submission timestamp, with no per-peer cap on queue occupancy. A single malicious P2P peer can flood the queue up to its 256 MB global ceiling via the relay protocol, monopolizing the verification workers and causing legitimate transactions from other peers or local RPC users to be delayed or rejected outright.

---

### Finding Description

`VerifyQueue` (`tx-pool/src/component/verify_queue.rs`) is the shared staging area that holds all transactions awaiting CKB-VM script verification before admission to the main tx-pool. Its two structural properties create the vulnerability:

**1. Global-only size cap, no per-peer cap.**
The only admission guard is a single global byte counter checked against `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` (256 MB). [1](#0-0) 

`add_tx()` rejects a new entry only when the global total would be exceeded; there is no per-peer accounting at all. [2](#0-1) 

**2. Strict FIFO dequeue, no fairness.**
`peek()` iterates `iter_by_added_time()` — pure wall-clock insertion order — with no per-peer interleaving or weighted scheduling. [3](#0-2) 

**Relay-side injection path.**
The relay protocol rate-limits each peer to 30 `RelayTransactions` messages per second (keyed by `(peer, message_type)`), and each message may carry up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` transactions and `MAX_RELAY_TXS_BYTES_PER_BATCH = 1 MiB` of data. [4](#0-3) 

`TransactionsProcess::execute()` submits every accepted transaction directly to the verify queue via `submit_remote_tx()` with no per-peer queue-occupancy check. [5](#0-4) 

`submit_remote_tx()` calls `resumeble_process_tx_and_notify_full_reject()`, which calls `add_tx()` on the shared `VerifyQueue`. [6](#0-5) 

**Arithmetic.** At 30 messages/s × 1 MiB/message the attacker injects ≈ 30 MiB/s. The 256 MiB queue fills in ≈ 8–9 seconds. After that, every new transaction — from any peer or local RPC caller — is rejected with `Reject::Full` until the attacker's backlog drains. [7](#0-6) 

---

### Impact Explanation

Once the queue is saturated by one peer:

- **Rejection**: All subsequent `submit_remote_tx` and `submit_local_tx` calls return `Reject::Full`, surfaced to RPC callers as `PoolIsFull (-1106)`.
- **Starvation before saturation**: Because dequeue is strict FIFO, any legitimate transaction that does enter the queue is processed only after all earlier attacker transactions complete CKB-VM verification — which can consume up to `max_tx_verify_cycles = 70,000,000` cycles per transaction.
- **Verification worker monopoly**: The fixed pool of `max_tx_verify_workers` threads (default ¾ of CPU cores) is fully occupied with attacker transactions; the `OnlySmallCycleTx` worker role provides cycle-size segregation but no per-peer fairness. [8](#0-7) 

---

### Likelihood Explanation

- **No privilege required**: Any peer that completes the standard P2P handshake can send `RelayTransactionHashes` → receive `GetRelayTransactions` → reply with `RelayTransactions`. No key, stake, or miner role is needed.
- **Low cost**: Transactions need not be valid on-chain; they only need to pass the non-contextual checks in `non_contextual_verify()` before being enqueued. Script verification happens inside the queue, so invalid scripts still occupy queue space and worker time until they fail.
- **Rate limiter is insufficient**: 30 relay messages/s × 1 MiB/message = 30 MiB/s injection rate is far above what is needed to fill the 256 MiB queue in a short window. [9](#0-8) 

---

### Recommendation

**Short term**: Track per-peer byte occupancy inside `VerifyQueue`. In `add_tx()`, reject a new entry if the submitting peer already accounts for more than a configurable fraction (e.g., 1/N peers) of `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`.

**Long term**: Replace the FIFO dequeue in `peek()` with a fair-queuing discipline (e.g., weighted round-robin over peer buckets). This ensures that no single peer can delay transactions from all other peers regardless of submission rate.

---

### Proof of Concept

1. Connect a custom peer to a CKB node via the RelayV3 protocol.
2. Construct N minimal transactions (each passing `non_contextual_verify`, e.g., using an always-success lock script hash that the node cannot immediately resolve).
3. Send `RelayTransactionHashes` messages (up to 32767 hashes each, 30/s) to announce all N hashes.
4. When the node replies with `GetRelayTransactions`, respond with `RelayTransactions` messages (up to 1 MiB each, 30/s).
5. `TransactionsProcess::execute()` calls `submit_remote_tx()` for each transaction, which calls `VerifyQueue::add_tx()`.
6. After ≈ 9 seconds, `VerifyQueue::total_tx_size` reaches 256 MB; `is_full()` returns `true`.
7. Any subsequent `send_transaction` RPC call from a legitimate user returns `PoolIsFull (-1106)`.
8. Transactions already in the queue from the legitimate user (if any) are processed only after all attacker transactions ahead of them in FIFO order complete verification. [10](#0-9) [11](#0-10)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L56-76)
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

impl VerifyQueue {
    /// Create a new VerifyQueue
    pub(crate) fn new(large_cycle_threshold: u64) -> Self {
        VerifyQueue {
            inner: MultiIndexVerifyEntryMap::default(),
            ready_rx: Arc::new(Notify::new()),
            total_tx_size: 0,
            large_cycle_threshold,
        }
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L179-194)
```rust
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

**File:** sync/src/relayer/mod.rs (L59-98)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;

type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
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
```

**File:** sync/src/relayer/transactions_process.rs (L37-96)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };

        if txs.is_empty() {
            return Status::ok();
        }

        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }

        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });

        Status::ok()
    }
```

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

**File:** tx-pool/src/verify_mgr.rs (L179-203)
```rust
        let worker_num = service.tx_pool_config.max_tx_verify_workers;
        let workers: Vec<_> = (0..worker_num)
            .map({
                let tasks = Arc::clone(&service.verify_queue);
                let signal_exit = signal_exit.clone();
                move |idx| {
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
                    };
                    let (child_tx, child_rx) = watch::channel(ChunkCommand::Resume);
                    (
                        child_tx,
                        Worker::new(
                            service.clone(),
                            Arc::clone(&tasks),
                            child_rx,
                            signal_exit.clone(),
                            role,
                        ),
                    )
                }
            })
            .collect();
```
