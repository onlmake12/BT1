### Title
VerifyQueue Lacks Per-Peer Admission Limit, Enabling Large-Cycle Transaction Flood to DOS Tx-Pool Admission — (`tx-pool/src/component/verify_queue.rs`)

---

### Summary

The `VerifyQueue` in CKB's tx-pool is a pre-verification staging buffer with a global 256 MB size cap and no per-peer submission limit. An unprivileged P2P peer can flood the queue with large-cycle transactions that occupy worker threads for an extended period (the "waiting period" analog). While the queue is saturated, all new transaction submissions — including legitimate relay transactions, local RPC submissions, and miner proposal transactions — are rejected with `Reject::Full`, causing a sustained DOS of tx-pool admission.

---

### Finding Description

`VerifyQueue` is the staging area all transactions must pass through before entering the main pending pool. It enforces a single global byte-size cap:

```
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
``` [1](#0-0) 

The `is_full` check is purely global — there is no per-peer accounting:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [2](#0-1) 

When `is_full` returns `true`, `add_tx` immediately returns `Reject::Full`:

```rust
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
``` [3](#0-2) 

The `VerifyMgr` spawns workers that drain the queue. One worker (`OnlySmallCycleTx`) is dedicated to small-cycle transactions and **skips** large-cycle entries entirely:

```rust
let role = if idx == 0 && worker_num > 1 {
    WorkerRole::OnlySmallCycleTx
} else {
    WorkerRole::SubmitTimeFirst
};
``` [4](#0-3) 

```rust
let entry = {
    let mut tasks = self.tasks.write().await;
    match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
``` [5](#0-4) 

`pop_front(only_small_cycle=true)` filters out large-cycle entries:

```rust
let entry = if only_small_cycle {
    self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
} else {
    self.inner.iter_by_added_time().next()
};
``` [6](#0-5) 

A transaction is classified as large-cycle based on the peer-declared cycle count:

```rust
let is_large_cycle = remote
    .map(|(cycles, _)| cycles > self.large_cycle_threshold)
    .unwrap_or(false);
``` [7](#0-6) 

The `large_cycle_threshold` comes from `pool_config.max_tx_verify_cycles`. An attacker submitting transactions with declared cycles just above this threshold causes them to be classified as large-cycle, skipped by the fast worker, and processed slowly by the remaining workers — creating an extended "waiting period" during which the queue remains saturated.

All three admission paths fail when the queue is full:

- `submit_remote_tx` (P2P relay path): [8](#0-7) 

- `notify_tx` (local/RPC proposal path): [9](#0-8) 

- `process_orphan_tx` (orphan resolution path — orphans that become resolvable cannot re-enter the queue): [10](#0-9) 

The existing tests confirm this behavior explicitly:

```rust
async fn submit_remote_tx_notifies_relayer_when_verify_queue_is_full() {
    ...
    assert!(matches!(ret, Err(crate::error::Reject::Full(_))));
``` [11](#0-10) 

---

### Impact Explanation

When the `VerifyQueue` is saturated:

1. **Remote transaction relay is blocked**: Peers cannot relay transactions to this node. The node's mempool stops growing, degrading its ability to participate in block propagation.
2. **Local RPC `send_transaction` fails**: Users and applications submitting transactions via RPC receive `PoolIsFull` errors.
3. **Orphan resolution is blocked**: Transactions waiting in the orphan pool that become resolvable (their parent arrives) cannot be promoted to the verify queue, causing them to remain stranded indefinitely.
4. **Miner proposal transactions are blocked**: `notify_tx` (used for proposal-phase transactions) fails, degrading block assembly quality.

The `VerifyQueue` has no automatic eviction or timeout — it only drains as workers complete verification. Large-cycle transactions hold queue space for the full duration of their script execution, which can be substantial.

---

### Likelihood Explanation

The attack is reachable by any unprivileged P2P peer. The attacker connects to the target node and relays transactions via the standard `RelayV3` protocol. The maximum transaction size is 512 KB (`TRANSACTION_SIZE_LIMIT = 512 * 1_000`), so filling the 256 MB queue requires approximately 512 transactions. [12](#0-11) 

Because there is no per-peer quota in `add_tx`, a single attacker peer can consume the entire queue budget. The attacker must hold enough CKB to construct valid transactions, but the cost is bounded while the disruption is sustained for as long as the attacker keeps submitting new transactions to replace those that drain out.

---

### Recommendation

1. **Add a per-peer byte quota** inside `add_tx` in `VerifyQueue`. Track `total_tx_size_by_peer: HashMap<PeerIndex, usize>` and reject submissions from a peer that exceed a per-peer cap (e.g., `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / MAX_PEERS`).
2. **Evict large-cycle transactions from slow peers** when the queue approaches capacity, similar to how `remove_txs_by_peer` already exists for cleanup.
3. **Apply a rate limit per peer** at the relay layer before transactions reach the `VerifyQueue`, analogous to the rate limiter already present in the hole-punching protocol. [13](#0-12) 

---

### Proof of Concept

```
1. Attacker connects to target node as a P2P peer via RelayV3.
2. Attacker holds ~512 valid UTXOs and constructs 512 transactions,
   each ~512 KB in serialized size, with scripts that consume cycles
   just above `max_tx_verify_cycles` (the large-cycle threshold).
3. Attacker relays all 512 transactions via RelayTransactions messages,
   declaring cycles = max_tx_verify_cycles + 1 for each.
4. Each transaction enters VerifyQueue::add_tx() and is classified
   is_large_cycle = true. The OnlySmallCycleTx worker skips them all.
   The SubmitTimeFirst workers verify them slowly.
5. total_tx_size approaches DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE (256 MB).
6. Any subsequent call to submit_remote_tx(), notify_tx(), or
   process_orphan_tx() returns Reject::Full.
7. Legitimate users' send_transaction RPC calls fail with PoolIsFull.
   Orphan transactions that become resolvable remain stranded.
   Miner proposal transactions cannot enter the queue.
8. Attacker continuously submits replacement transactions as old ones
   drain, maintaining the saturated state indefinitely.
``` [14](#0-13) [2](#0-1)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L187-191)
```rust
        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };
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

**File:** tx-pool/src/verify_mgr.rs (L130-132)
```rust
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
```

**File:** tx-pool/src/verify_mgr.rs (L185-190)
```rust
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
                    };
                    let (child_tx, child_rx) = watch::channel(ChunkCommand::Resume);
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

**File:** tx-pool/src/process.rs (L381-384)
```rust
    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
    }
```

**File:** tx-pool/src/process.rs (L604-624)
```rust
                    let orphan_id = orphan.tx.proposal_short_id();
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
                        }
                        Err(reject) => {
                            warn!(
                                "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );
                        }
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

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
