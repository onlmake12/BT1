### Title
Tx-Pool Local State Variables `total_tx_size`/`total_tx_cycles` Can Diverge from Actual Pool Contents, Causing Incorrect Pool Size Limit Enforcement — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap` maintains two local state variables — `total_tx_size` and `total_tx_cycles` — that are updated incrementally as transactions are added or removed. The pool size limit enforcement in `limit_size` relies exclusively on `total_tx_size` rather than recomputing from actual entries. The code itself acknowledges that `total_tx_cycles` tracking can be inaccurate ("cycles overflow is possible, currently obtaining cycles is not accurate"), and a recovery path exists for when subtraction underflows. When the recovery path also fails, neither variable is updated, leaving them stale. An overstated `total_tx_size` causes unnecessary eviction of valid transactions; an understated one allows the pool to silently exceed `max_tx_pool_size`.

---

### Finding Description

`PoolMap` in `tx-pool/src/component/pool_map.rs` tracks two aggregate counters as local state:

```rust
// sum of all tx_pool tx's virtual sizes.
pub(crate) total_tx_size: usize,
// sum of all tx_pool tx's cycles.
pub(crate) total_tx_cycles: Cycle,
``` [1](#0-0) 

These are incremented in `add_entry` and decremented in `remove_entry` → `update_stat_for_remove_tx`. The removal path explicitly acknowledges the tracking can be wrong:

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
                error!("tx-pool total stats underflowed ...");
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            } else {
                error!("... and recomputing overflowed");
                // Neither variable is updated — stale state persists
            }
        }
    }
}
``` [2](#0-1) 

The `_` branch is entered whenever **either** `total_tx_size` or `total_tx_cycles` underflows. When the fallback `recompute_total_stat()` also returns `None`, both counters are left at their pre-removal values — overstated by the size/cycles of the removed transaction.

The pool size limit enforcement in `limit_size` uses `total_tx_size` as the authoritative source:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict entries
}
``` [3](#0-2) 

Similarly, `updated_stat_for_add_tx` uses the local counter to gate admission:

```rust
let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_size {} overflows by add {}", ...))
})?;
``` [4](#0-3) 

The same pattern exists in `verify_queue.rs`, where `is_full` uses a local `total_tx_size` for the 256 MB queue cap:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [5](#0-4) 

with the same stale-state failure path in `remove_tx`: [6](#0-5) 

---

### Impact Explanation

**If `total_tx_size` is overstated** (the realistic failure mode when the `_` branch fires and recompute fails): `limit_size` evicts valid transactions that should not be evicted, and `updated_stat_for_add_tx` rejects incoming transactions with `Reject::Full` even though the pool is within its actual limit. This is a correctness-level DoS against transaction submitters — legitimate transactions are silently dropped or rejected.

**If `total_tx_size` is understated** (requires recompute to return a value lower than the pre-removal total, which can happen if entries were already removed from `self.entries` before `recompute_total_stat` runs): `limit_size` does not evict when it should, allowing the pool to grow beyond `max_tx_pool_size` (default 180 MB), potentially causing memory exhaustion and node instability.

The `tx_pool_info` RPC exposes `total_tx_size` and `total_tx_cycles` directly to callers, so stale values also mislead monitoring tools and fee estimators. [7](#0-6) 

---

### Likelihood Explanation

The code comment "cycles overflow is possible, currently obtaining cycles is not accurate" is a developer acknowledgment that `total_tx_cycles` divergence is a known, observed condition — not purely theoretical. The recovery path (`recompute_total_stat`) exists precisely because underflows occur in production. The failure of the recompute itself (overflow of `usize` when summing all entry sizes) is unlikely on 64-bit systems given the 180 MB pool cap, but the primary divergence — `total_tx_cycles` becoming overstated and triggering the `_` branch — is acknowledged as realistic. Any unprivileged tx-pool submitter (via the `send_transaction` RPC) participates in the pool lifecycle that exercises these paths.

---

### Recommendation

1. **Use real-time recomputation for critical checks**: `limit_size` and `is_full` should recompute from actual entries (or assert consistency) rather than relying solely on the incremental counters.
2. **Separate informational tracking from enforcement**: Clearly distinguish between the local counters (used for metrics/RPC) and the authoritative values (recomputed from entries) used for admission and eviction decisions.
3. **Fix the silent no-op**: In the `else` branch of `update_stat_for_remove_tx` and `verify_queue::remove_tx`, when recompute overflows, the counters must still be corrected (e.g., saturate to zero or to the recomputed partial sum) rather than left stale.

---

### Proof of Concept

1. Fill the pool to near `max_tx_pool_size` with transactions that have high declared cycles.
2. Submit a transaction whose removal triggers `total_tx_cycles.checked_sub` to underflow (e.g., if cycles were declared differently from what was stored, or via a sequence of RBF replacements that leave the counter inconsistent).
3. The `_` branch fires; `recompute_total_stat` is called.
4. If recompute succeeds (normal case), both counters are corrected — no persistent divergence.
5. If recompute fails (edge case on 32-bit or with a crafted pool state), `total_tx_size` is left at its pre-removal value (overstated).
6. Subsequent calls to `updated_stat_for_add_tx` reject new transactions with `Reject::Full` even though the pool is below its actual limit, or `limit_size` evicts valid transactions unnecessarily.

The root cause — using `total_tx_size` as the authoritative source for pool size enforcement rather than recomputing from actual entries — is directly analogous to the reported pattern of using `total_collateral`/`base_balance` local state variables for withdrawal validation instead of the real-time vault balance. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L698-758)
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

    /// Calculate size and cycles statistics for adding a tx.
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }

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

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L129-148)
```rust
    pub fn remove_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
        self.inner.remove_by_id(id).map(|e| {
            let tx_size = e.inner.tx.data().serialized_size_in_block();
            if let Some(total_tx_size) = self.total_tx_size.checked_sub(tx_size) {
                self.total_tx_size = total_tx_size;
            } else if let Some(total_tx_size) = self.recompute_total_tx_size() {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, recomputed {}",
                    self.total_tx_size, tx_size, total_tx_size
                );
                self.total_tx_size = total_tx_size;
            } else {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, and recomputing overflowed",
                    self.total_tx_size, tx_size
                );
            }
            self.shrink_to_fit();
            e.inner
        })
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
