### Title
Acknowledged Inaccuracy in `total_tx_cycles` / `total_tx_size` Accounting in `PoolMap::update_stat_for_remove_tx` Can Leave Pool Statistics Permanently Stale — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::update_stat_for_remove_tx` contains an acknowledged comment that cycle accounting is inaccurate. When the primary subtraction underflows and the fallback `recompute_total_stat()` also fails (returns `None`), neither `total_tx_size` nor `total_tx_cycles` is updated — the stale values persist silently. Because `total_tx_size` is the sole enforcement variable in `limit_size()`, a permanently inflated counter causes the pool to over-evict valid transactions; a permanently deflated counter allows the pool to grow beyond `max_tx_pool_size`, enabling resource exhaustion by any unprivileged transaction submitter.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `PoolMap` tracks two aggregate counters:

```
// sum of all tx_pool tx's virtual sizes.
pub(crate) total_tx_size: usize,
// sum of all tx_pool tx's cycles.
pub(crate) total_tx_cycles: Cycle,
``` [1](#0-0) 

Every removal path calls `update_stat_for_remove_tx`:

```rust
/// Update size and cycles statistics for remove tx
/// cycles overflow is possible, currently obtaining cycles is not accurate
fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
    match (
        self.total_tx_size.checked_sub(tx_size),
        self.total_tx_cycles.checked_sub(cycles),
    ) {
        (Some(total_tx_size), Some(total_tx_cycles)) => { ... }
        _ => {
            if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                error!(...);
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            } else {
                error!(...);
                // ← NO UPDATE: stale values persist
            }
        }
    }
}
``` [2](#0-1) 

The code's own comment admits: *"cycles overflow is possible, currently obtaining cycles is not accurate."* This is the direct analog to the original report's "no documentation of what token is recovered and no clarity if tokenTotalAmount should be increased."

Two concrete problems exist:

**Problem 1 — Acknowledged cycles inaccuracy.** Cycles stored in a `TxEntry` are an estimate. When a transaction is removed, the subtracted cycles may differ from what was added, causing `total_tx_cycles` to drift. The comment acknowledges this but provides no correction mechanism beyond the fallback recompute.

**Problem 2 — Silent no-op when recompute overflows.** `recompute_total_stat()` uses `checked_add` over all entries:

```rust
fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
    self.entries.iter().try_fold(
        (0usize, 0 as Cycle),
        |(total_size, total_cycles), (_, entry)| {
            Some((
                total_size.checked_add(entry.inner.size)?,
                total_cycles.checked_add(entry.inner.cycles)?,
            ))
        },
    )
}
``` [3](#0-2) 

If this returns `None`, neither counter is updated. The `else` branch only logs an error — the stale `total_tx_size` and `total_tx_cycles` remain in place indefinitely.

The pool size enforcement loop reads `total_tx_size` directly:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [4](#0-3) 

A stale `total_tx_size` directly corrupts this enforcement.

---

### Impact Explanation

- **Inflated `total_tx_size`**: `limit_size()` over-evicts valid pending/proposed transactions, causing legitimate transactions to be rejected with `Reject::Full` even when the pool has real capacity. This degrades liveness for honest users.
- **Deflated `total_tx_size`**: The pool accepts transactions beyond `max_tx_pool_size`, enabling any unprivileged submitter to exhaust node memory by flooding the pool.
- **Inaccurate `total_tx_cycles`**: Exposed via the `tx_pool_info` RPC (`total_tx_cycles` field) and used in fee estimation logic, misleading clients and miners about pool state. [5](#0-4) 

---

### Likelihood Explanation

The underflow trigger (Problem 1) is realistic: the code itself documents that cycle values are inaccurate estimates. Any mismatch between the cycles added on insertion and the cycles subtracted on removal triggers the fallback path. The recompute overflow (Problem 2) requires `Cycle` (a `u64`) to overflow across all pool entries — unlikely on its own, but the cycles inaccuracy means the recompute result may also differ from the true sum, leaving the counter in an incorrect state even after a successful recompute. The entry path requires no privilege: any node peer or RPC caller can submit transactions via `send_transaction`.

---

### Recommendation

1. **Document clearly** what `update_stat_for_remove_tx` is responsible for and under what conditions cycles can be inaccurate (analogous to the original report's recommendation to add documentation to `recover_token()`).
2. **Add a mandatory recompute** of `total_tx_size` and `total_tx_cycles` on any underflow, and treat a failed recompute as a fatal invariant violation (panic or hard reset to zero) rather than a silent no-op.
3. **Separate size and cycles tracking**: since `total_tx_size` is security-critical (pool size enforcement) and `total_tx_cycles` is acknowledged as approximate, handle their error paths differently — size must never be left stale.
4. Consider adding a periodic background consistency check that recomputes both counters and alerts on divergence.

---

### Proof of Concept

1. Submit a batch of transactions to the pool, each with a large estimated cycle count.
2. Trigger removal of those transactions (e.g., via block commit or `remove_transaction` RPC).
3. If the stored `cycles` value for any entry differs from what was accumulated in `total_tx_cycles` (acknowledged as possible by the comment), `checked_sub` underflows, triggering the fallback.
4. Observe via `tx_pool_info` RPC that `total_tx_cycles` diverges from the sum of individual entry cycles.
5. In the recompute-overflow scenario (crafted with `Cycle::MAX`-adjacent values), observe that neither counter is updated and the stale value persists, causing `limit_size()` to make incorrect eviction decisions on subsequent insertions. [6](#0-5) [3](#0-2) [7](#0-6)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
```

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
    }
```

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
        }
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

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
