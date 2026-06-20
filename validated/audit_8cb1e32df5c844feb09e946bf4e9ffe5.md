### Title
Verify Queue Admission Check Ignores Accumulated Pool Map Size, Allowing Memory Limit Bypass — (File: `tx-pool/src/component/verify_queue.rs`)

### Summary

The `VerifyQueue` admission check (`is_full`) validates only against its own internal `total_tx_size` counter and a hardcoded 256 MB ceiling. It never accounts for the transactions already accumulated in the `PoolMap`. Because the two structures track memory independently, an unprivileged submitter can cause total tx-pool memory consumption to reach `verify_queue_limit + pool_map_limit` (up to ~436 MB by default), silently exceeding the operator-configured `max_tx_pool_size`.

### Finding Description

The CKB tx-pool is split into two independent size-tracked structures:

**`VerifyQueue`** — a staging area for transactions undergoing script verification.

```
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

Its admission gate:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [1](#0-0) [2](#0-1) 

This check only subtracts the verify queue's own `total_tx_size` from the hardcoded 256 MB ceiling. It has no visibility into the `PoolMap`'s current `total_tx_size`.

**`PoolMap`** — the main mempool, enforced separately:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [3](#0-2) 

`limit_size()` only evicts from the pool map when the pool map alone exceeds `max_tx_pool_size`. It never reads the verify queue's size.

The `TxPoolInfo` struct reports `total_tx_size` as only the pool map's counter:

```rust
total_tx_size: tx_pool.pool_map.total_tx_size,
``` [4](#0-3) 

The verify queue's byte footprint is reported only as a count (`verify_queue_size: verify_queue.len()`), not as bytes, and is never compared against `max_tx_pool_size`. [5](#0-4) 

### Impact Explanation

The two limits are additive and never jointly enforced:

| Structure | Limit |
|---|---|
| `PoolMap` | `max_tx_pool_size` (default 180 MB, operator-configurable) |
| `VerifyQueue` | `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` (hardcoded 256 MB) |
| **Combined maximum** | **~436 MB** |

An operator who sets `max_tx_pool_size = 10 MB` to conserve memory on a constrained node still exposes up to 256 MB of additional memory to the verify queue. The configured limit is silently bypassed. Under sustained submission pressure this can cause:

- Resident memory far exceeding the operator's configured budget
- OOM-kill of the node process on memory-constrained deployments
- Degraded block assembly and relay performance while the verify queue drains

### Likelihood Explanation

The entry path is fully unprivileged. Any peer or RPC caller can invoke `send_transaction` (or the P2P relay path) to push transactions into the verify queue. No special role, key, or majority hash power is required. The attacker only needs to submit enough transactions to fill the verify queue while the pool map is already at or near its limit — a straightforward, low-cost operation. [6](#0-5) 

### Recommendation

1. **Unified admission check**: Before inserting into the verify queue, compute the combined size:

```rust
pub fn is_full(&self, add_tx_size: usize, pool_map_total: usize) -> bool {
    self.total_tx_size
        .saturating_add(pool_map_total)
        .saturating_add(add_tx_size)
        >= self.max_combined_size
}
```

Pass `tx_pool.pool_map.total_tx_size` from the caller so the verify queue's gate reflects total accumulated obligations.

2. **Respect `max_tx_pool_size`**: Replace the hardcoded `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` with a value derived from the operator's `max_tx_pool_size` configuration (e.g., `max_tx_pool_size * 1.5` as a staging headroom), so the combined ceiling scales with the operator's intent.

3. **Expose combined size in `TxPoolInfo`**: Report `verify_queue_total_tx_size` in bytes (not just count) so operators and monitoring tools can observe the true memory footprint.

### Proof of Concept

1. Configure a node with `max_tx_pool_size = 10_000_000` (10 MB).
2. Fill the `PoolMap` to its 10 MB limit via normal transaction submission.
3. Continue submitting transactions. Each new transaction enters `VerifyQueue.add_tx()`, which calls `is_full()` checking only `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size` (256 MB − 0 = 256 MB available).
4. The verify queue accepts up to 256 MB of additional transactions.
5. Total resident memory for the tx-pool reaches ~266 MB — 26× the configured limit — while `get_tx_pool_info` reports `total_tx_size` at only 10 MB, giving no indication of the true memory pressure. [1](#0-0) [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/component/verify_queue.rs (L196-237)
```rust
    /// If the queue did not have this tx present, true is returned.
    /// If the queue did have this tx present, false is returned.
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** tx-pool/src/service.rs (L1083-1097)
```rust
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
```

**File:** tx-pool/src/component/pool_map.rs (L60-75)
```rust
pub struct PoolMap {
    /// The pool entries with different kinds of sort strategies
    pub(crate) entries: MultiIndexPoolEntryMap,
    /// All the deps, header_deps, inputs, outputs relationships
    pub(crate) edges: Edges,
    /// All the parent/children relationships
    pub(crate) links: TxLinksMap,
    pub(crate) max_ancestors_count: usize,
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
}
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-13)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```
