Audit Report

## Title
RBF Replacement Always Resets Pool Entry Timestamp, Enabling Indefinite Expiry Bypass - (File: tx-pool/src/component/entry.rs, tx-pool/src/process.rs, tx-pool/src/pool.rs)

## Summary
`TxEntry::new` unconditionally stamps the current wall-clock time as the entry's `timestamp`. When a transaction is replaced via RBF, the replacement entry is always created with `TxEntry::new`, discarding the original entry's timestamp entirely. This allows an attacker to repeatedly replace their own transaction just before the `expiry_hours` window closes, resetting the expiry clock indefinitely at near-zero marginal cost and permanently defeating the `remove_expired` eviction mechanism.

## Finding Description
`TxEntry::new` at [1](#0-0)  always calls `unix_time_as_millis()` for the timestamp. The alternative constructor `new_with_timestamp` exists at [2](#0-1)  and accepts an explicit timestamp, but is never called in the RBF path.

In `_process_tx`, the new entry is always created via `TxEntry::new(rtx, verified.cycles, fee, tx_size)` at [3](#0-2)  — no timestamp from the conflicting entry is retrieved or forwarded.

`process_rbf` at [4](#0-3)  removes all conflicting entries via `remove_entry_and_descendants` and discards them entirely, with no timestamp extraction before removal.

`remove_expired` at [5](#0-4)  uses the condition `self.expiry + entry.inner.timestamp < now_ms`, so a freshly-stamped replacement entry always has its deadline reset to `now + expiry_hours`.

`check_rbf` at [6](#0-5)  validates fee rules and descendant limits but imposes no constraint on the original entry's age or timestamp — it does not carry the old timestamp forward.

The minimum replacement fee is `sum(replaced_txs.fee) + min_rbf_rate.fee(size)` as computed in [7](#0-6) . With `min_rbf_rate = 1500 shannons/KB` and a ~200-byte tx, each replacement costs ~300 shannons. Fees grow linearly across replacements but remain negligible in absolute terms.

## Impact Explanation
This matches **High: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker can occupy tx-pool slots indefinitely at near-zero cost, defeating the expiry eviction mechanism designed to reclaim pool resources. The pool has a fixed `max_tx_pool_size`; with the expiry mechanism bypassed, an attacker running multiple such chains can crowd out legitimate transactions. Any UTXO consumed by the attacker's transaction is kept locked in the pool indefinitely, requiring any third party to continuously outbid the attacker.

## Likelihood Explanation
- Triggerable by any unprivileged user via the `send_transaction` RPC or P2P relay.
- Requires only knowledge of the publicly documented default `expiry_hours = 12` and `min_rbf_rate`.
- RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is opt-in, but nodes that enable it are fully exposed.
- Cost is ~300 shannons per 12-hour cycle, growing linearly but remaining negligible indefinitely.
- No special knowledge, victim mistakes, or external dependencies required.

## Recommendation
Preserve the original entry's timestamp when a transaction is replaced via RBF. In `process_rbf` (`process.rs:190-235`), extract the minimum (oldest) `timestamp` among all conflicting entries being evicted before they are removed. Pass this inherited timestamp to `TxEntry::new_with_timestamp(...)` instead of `TxEntry::new` at `process.rs:751` in the RBF replacement path. The `new_with_timestamp` constructor already exists at [2](#0-1)  and requires no modification.

## Proof of Concept
1. Configure node: `min_fee_rate = 1000`, `min_rbf_rate = 1500`, `expiry_hours = 12`.
2. Submit **T1** spending UTXO `[A]` with fee 1000 shannons/KB. Pool records `T1.timestamp = T0`.
3. At `T0 + 11h55m`, submit **T2** spending the same UTXO `[A]` with fee ≥ 1500 shannons/KB. `check_rbf` passes; `process_rbf` removes T1; `_submit_entry` inserts T2 with `T2.timestamp = T0 + 11h55m`.
4. `remove_expired` at `T0 + 12h`: T1 is already gone; T2's deadline is `T0 + 11h55m + 12h` — safely in the future.
5. Repeat step 3 every ~11.9 hours. UTXO `[A]` remains locked in the pool indefinitely. Total cost after N cycles is approximately `N*(N+1)/2 * 300` shannons — negligible for any practical duration.
6. Verify via `get_pool_tx_detail_info` RPC after each replacement to confirm the timestamp resets and the entry never expires.

### Citations

**File:** tx-pool/src/component/entry.rs (L48-50)
```rust
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }
```

**File:** tx-pool/src/component/entry.rs (L52-75)
```rust
    /// Create new transaction pool entry with specified timestamp
    pub fn new_with_timestamp(
        rtx: Arc<ResolvedTransaction>,
        cycles: Cycle,
        fee: Capacity,
        size: usize,
        timestamp: u64,
    ) -> Self {
        TxEntry {
            rtx,
            cycles,
            size,
            fee,
            timestamp,
            ancestors_size: size,
            ancestors_fee: fee,
            ancestors_cycles: cycles,
            descendants_fee: fee,
            descendants_size: size,
            descendants_cycles: cycles,
            descendants_count: 1,
            ancestors_count: 1,
        }
    }
```

**File:** tx-pool/src/process.rs (L203-232)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
```

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/pool.rs (L102-114)
```rust
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
```

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L574-679)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }

        let tx_cells_deps: Vec<OutPoint> = entry
            .transaction()
            .cell_deps_iter()
            .map(|c| c.out_point())
            .collect();
        for entry in all_conflicted.iter() {
            let hash = entry.inner.transaction().hash();
            if tx_cells_deps.iter().any(|pt| pt.tx_hash() == hash) {
                return Err(Reject::RBFRejected(
                    "new Tx contains cell deps from conflicts".to_string(),
                ));
            }
        }

        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }

        Ok(conflict_ids)
    }
```
