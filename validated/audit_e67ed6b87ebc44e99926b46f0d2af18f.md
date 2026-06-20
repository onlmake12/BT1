### Title
Tx-Pool Size Accounting Excludes `verify_queue` Transactions, Allowing Memory Usage to Silently Exceed `max_tx_pool_size` - (File: `tx-pool/src/component/verify_queue.rs`, `tx-pool/src/pool.rs`, `tx-pool/src/service.rs`)

---

### Summary

The CKB tx-pool maintains two independent size-tracked structures: `pool_map` (the main mempool) and `verify_queue` (the pre-verification staging queue). The size limit enforcement (`limit_size`) and the `tx_pool_info` RPC both account only for `pool_map.total_tx_size`, completely ignoring `verify_queue.total_tx_size`. An unprivileged attacker can flood the `verify_queue` with large transactions (up to its hardcoded 256 MB cap) while the `pool_map` is simultaneously at its configured `max_tx_pool_size` (default 180 MB), causing the node to silently consume up to ~436 MB of tx-pool memory — 2.4× the operator-configured limit — with no eviction triggered and no accurate accounting visible via RPC.

---

### Finding Description

The tx-pool is split into two distinct queues:

**`VerifyQueue`** — a staging area where incoming transactions wait for background script verification before being admitted to the main pool. It has its own independent size cap:

```
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
``` [1](#0-0) 

Its fullness check is entirely self-contained:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [2](#0-1) 

**`PoolMap`** — the main mempool, enforced by `limit_size` which evicts entries when `pool_map.total_tx_size > config.max_tx_pool_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries from pool_map only
``` [3](#0-2) 

The `TxPoolService::info()` function, which populates the `tx_pool_info` RPC response, reads only `pool_map.total_tx_size`:

```rust
TxPoolInfo {
    ...
    total_tx_size: tx_pool.pool_map.total_tx_size,
    total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
    ...
    verify_queue_size: verify_queue.len(),  // only count, not byte size
}
``` [4](#0-3) 

The `verify_queue.total_tx_size` is never included in the reported `total_tx_size`, and the `limit_size` eviction loop never considers it. The two limits are enforced in complete isolation.

---

### Impact Explanation

An unprivileged attacker can simultaneously saturate both queues:

1. Submit enough large transactions to fill `verify_queue` to its 256 MB cap. These transactions are accepted because `verify_queue.is_full()` only checks its own internal counter.
2. Independently, the `pool_map` can be at its configured `max_tx_pool_size` (default 180 MB) with legitimate or attacker-controlled transactions.
3. Total tx-pool memory consumption reaches up to **436 MB** while `tx_pool_info` reports only the `pool_map` portion (~180 MB).
4. The `limit_size` eviction loop never fires to compensate for the `verify_queue` load, because it only reads `pool_map.total_tx_size`.

On memory-constrained nodes (e.g., nodes configured with `max_tx_pool_size = 180MB` expecting total tx-pool memory to be bounded near that value), this silent 256 MB overflow from the `verify_queue` can trigger OOM conditions or degrade node performance. Operators monitoring `total_tx_size` via `tx_pool_info` RPC receive a systematically understated figure, making the problem invisible until the node crashes or becomes unresponsive. [5](#0-4) 

---

### Likelihood Explanation

The entry path requires no privilege: any peer can submit transactions via the P2P relay protocol or the `send_transaction` / `submit_local_tx` RPC. Transactions are accepted into `verify_queue` before any script verification occurs. A single attacker with enough CKB to construct large valid-looking transactions (or even transactions that will fail verification but still occupy queue space during the verification window) can fill the `verify_queue` to its 256 MB cap. The attack is repeatable as long as the attacker can keep submitting new transactions faster than the verification workers drain the queue. [6](#0-5) 

---

### Recommendation

1. **Unified size accounting**: The `limit_size` eviction check and the `TxPoolInfo` struct should include `verify_queue.total_tx_size` in the total, so that the combined size of both queues is bounded by `max_tx_pool_size` (or a clearly documented combined limit).
2. **Expose verify_queue byte size via RPC**: The `tx_pool_info` response currently exposes `verify_queue_size` (count only). It should also expose `verify_queue_tx_size` in bytes so operators can observe actual memory usage.
3. **Coordinated eviction**: When `pool_map` is at its limit and `verify_queue` is also near its limit, the node should either reject new `verify_queue` entries earlier or evict `pool_map` entries to make room for the combined load.

---

### Proof of Concept

1. Configure a CKB node with `max_tx_pool_size = 180_000_000` (180 MB, the default).
2. Flood the node with large transactions via P2P relay or `send_transaction` RPC. Each transaction is accepted into `verify_queue` until `verify_queue.total_tx_size` approaches 256 MB.
3. Simultaneously, ensure `pool_map.total_tx_size` is near 180 MB (e.g., via previously verified transactions).
4. Observe via `tx_pool_info` that `total_tx_size` reports only ~180 MB (the `pool_map` portion), while actual process memory shows ~436 MB consumed by tx-pool structures.
5. The `limit_size` loop never triggers to evict `pool_map` entries, because `pool_map.total_tx_size` alone does not exceed `max_tx_pool_size` — the `verify_queue` overflow is invisible to the eviction logic. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/component/verify_queue.rs (L198-220)
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
```

**File:** tx-pool/src/pool.rs (L292-329)
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
    }
```

**File:** tx-pool/src/service.rs (L1078-1098)
```rust
    async fn info(&self) -> TxPoolInfo {
        let tx_pool = self.tx_pool.read().await;
        let orphan = self.orphan.read().await;
        let verify_queue = self.verify_queue.read().await;
        let tip_header = tx_pool.snapshot.tip_header();
        TxPoolInfo {
            tip_hash: tip_header.hash(),
            tip_number: tip_header.number(),
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
        }
    }
```
