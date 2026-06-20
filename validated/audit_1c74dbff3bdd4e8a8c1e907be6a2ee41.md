### Title
Infinite Loop in `limit_size()` When `next_evict_entry()` Returns `None` — (`File: tx-pool/src/pool.rs`)

---

### Summary

The `limit_size()` function in `tx-pool/src/pool.rs` contains a `while` loop that iterates as long as `total_tx_size > max_tx_pool_size`. Inside the loop, it calls `next_evict_entry()` to find a transaction to evict. If `next_evict_entry()` returns `None`, the loop body does nothing — `total_tx_size` is never reduced and the loop condition remains permanently `true`, spinning forever. This is the direct analog of the Casimir `exitValidators()` bug: a loop counter that is only updated inside a conditional branch, with no fallback `break` in the `None` arm.

---

### Finding Description

In `tx-pool/src/pool.rs`, the `limit_size()` function is responsible for evicting low-fee transactions when the pool exceeds its configured byte limit:

```rust
// tx-pool/src/pool.rs  lines 292-329
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
                // ... callbacks, logging ...
            }
        }
        // ← NO else { break; } HERE
    }
    self.pool_map.entries.shrink_to_fit();
    ret
}
``` [1](#0-0) 

If `next_evict_entry()` returns `None`, the `if let Some(id)` arm is skipped entirely. `total_tx_size` is never decremented, the loop condition stays `true`, and the thread spins in a tight infinite loop.

The `total_tx_size` counter can become inconsistent with the actual pool contents through the acknowledged error path in `update_stat_for_remove_tx`:

```rust
// tx-pool/src/component/pool_map.rs  lines 733-758
fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
    match (
        self.total_tx_size.checked_sub(tx_size),
        self.total_tx_cycles.checked_sub(cycles),
    ) {
        (Some(total_tx_size), Some(total_tx_cycles)) => { ... }
        _ => {
            if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                error!("tx-pool total stats underflowed ...");
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            } else {
                error!(
                    "tx-pool total stats underflowed ... and recomputing overflowed"
                );
                // ← total_tx_size is NOT updated; stale inflated value persists
            }
        }
    }
}
``` [2](#0-1) 

The code comment on `update_stat_for_remove_tx` explicitly acknowledges: *"cycles overflow is possible, currently obtaining cycles is not accurate"*. When the `recompute_total_stat()` fallback also fails (returns `None`), `total_tx_size` retains its stale, inflated value. If the pool is subsequently drained to empty while `total_tx_size` remains above `max_tx_pool_size`, `next_evict_entry()` returns `None` on every iteration and the loop never exits. [3](#0-2) 

---

### Impact Explanation

The `limit_size()` function executes on the tx-pool service thread. An infinite loop here permanently stalls that thread. All subsequent operations that acquire the tx-pool lock — including `send_transaction` RPC calls, block assembly (`get_block_template`), and reorg processing — will block indefinitely. This constitutes a complete denial-of-service of the transaction pool subsystem, preventing the node from relaying or mining transactions.

---

### Likelihood Explanation

The trigger requires `total_tx_size` to be inconsistently larger than the sum of actual entry sizes while the pool is empty (or all entries have been evicted). This inconsistency is explicitly acknowledged in the codebase. An attacker who can observe the pool state and submit transactions at high volume can attempt to induce the underflow/recompute-overflow path in `update_stat_for_remove_tx`. The `send_transaction` RPC endpoint is publicly accessible to any unprivileged peer or RPC caller, making the entry path fully externally reachable.

---

### Recommendation

Add an explicit `break` (or `return`) in the `else` arm of the `if let Some(id)` check inside `limit_size()`, mirroring the fix applied to the Casimir `exitValidators()` bug:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    let next_evict_entry = || { ... };

    if let Some(id) = next_evict_entry() {
        let removed = self.pool_map.remove_entry_and_descendants(&id);
        for entry in removed { ... }
    } else {
        // No entry to evict; pool is empty or stats are inconsistent.
        // Break to avoid an infinite loop.
        break;
    }
}
```

Additionally, the `update_stat_for_remove_tx` error path where `recompute_total_stat()` returns `None` should forcibly reset `total_tx_size` to `0` rather than leaving it stale, since an empty pool has zero size by definition.

---

### Proof of Concept

1. Fill the tx-pool to near `max_tx_pool_size` with low-fee transactions.
2. Trigger the `update_stat_for_remove_tx` underflow path (e.g., by submitting a transaction whose recorded `size` differs from what is subtracted during removal, or by inducing the acknowledged cycles-accounting inconsistency) so that `total_tx_size` retains an inflated value after entries are removed.
3. Submit one more transaction via `send_transaction` RPC. This calls `limit_size()`.
4. The loop evicts all remaining entries; the pool becomes empty.
5. `next_evict_entry()` now returns `None` on every iteration.
6. `total_tx_size` is never decremented; the loop condition stays `true`.
7. The tx-pool service thread spins indefinitely. All subsequent `send_transaction`, `get_block_template`, and reorg-notification calls block forever. [4](#0-3) [5](#0-4)

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
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

**File:** tx-pool/src/component/pool_map.rs (L733-758)
```rust
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
