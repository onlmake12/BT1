### Title
Transaction Pinning via Child-Transaction Injection Inflates `min_replace_fee`, Blocking RBF Replacements - (`tx-pool/src/pool.rs`)

### Summary

CKB's RBF (Replace-By-Fee) mechanism exposes a TOCTOU (Time-of-Check-Time-of-Use) griefing vector. A transaction owner can prevent their pending transaction from being replaced by frontrunning the replacement submission with low-fee child transactions that inflate the `min_replace_fee` above the replacement's fee, causing `check_rbf` to reject the replacement.

### Finding Description

The `get_transaction` RPC exposes a `min_replace_fee` field that callers use to determine the minimum fee required to replace a pending transaction via RBF. This value is computed by `min_replace_fee()` as the sum of fees of the target transaction and all its pool descendants, plus an extra RBF surcharge:

```
min_replace_fee = sum(fees of tx + all descendants) + min_rbf_rate * replacement_size
``` [1](#0-0) 

The `calculate_min_replace_fee` helper accumulates fees across all conflicted entries: [2](#0-1) 

When a replacement is submitted, `check_rbf` recomputes `all_conflicted` (the target tx plus all its current descendants) and rejects the replacement if its fee is below the freshly computed `min_replace_fee`: [3](#0-2) 

The attack window is the gap between when a replacer queries `min_replace_fee` via `get_transaction` and when they submit the replacement. During this window, the original transaction owner can inject up to `MAX_REPLACEMENT_CANDIDATES - 1` (99) low-fee child transactions spending the output of the target transaction. Each child adds its fee to the required `min_replace_fee`, raising it above the replacement's fee and causing `check_rbf` to return `Reject::RBFRejected`. [4](#0-3) 

The `min_replace_fee` field is explicitly surfaced in the RPC response type and documented as the value callers should use to construct a valid replacement: [5](#0-4) 

### Impact Explanation

A transaction owner can indefinitely block any third-party RBF replacement of their transaction without ever putting the transaction into a healthy (confirmed) state. Each time a replacement is attempted, the attacker re-injects children to re-inflate `min_replace_fee`. The attacker's cost is only the minimum fee for each child transaction (1000 shannons/KB by default), while the replacer must keep increasing their fee. This is a griefing/DoS on the RBF mechanism: the original transaction remains stuck in the pool, unconfirmable and unreplaceable, for as long as the attacker is willing to pay trivial child-transaction fees.

### Likelihood Explanation

RBF is enabled when `min_rbf_rate > min_fee_rate` (the default config sets `min_rbf_rate = 1500`, `min_fee_rate = 1000`). Any unprivileged tx-pool submitter can exploit this by calling `send_transaction` to inject child transactions. The `get_transaction` RPC is public and the `min_replace_fee` field is explicitly documented for use by replacers, making the TOCTOU window well-defined and easy to exploit. The attacker only needs to monitor the mempool and react faster than the replacer's submission, which is straightforward on a local or co-located node. [6](#0-5) 

### Recommendation

In `check_rbf`, when the replacement fee is sufficient to cover the direct conflict's fee plus `extra_rbf_fee` but falls short only because of newly injected descendants, the function should not hard-reject. Instead, it should proceed with the replacement using only the direct conflict (and descendants that existed at the time the replacer queried `min_replace_fee`). Concretely:

1. **Cap descendant inclusion**: Only include descendants that were present when `min_replace_fee` was last published, or
2. **Decouple descendant fees from the replacement threshold**: Require the replacement fee to cover only `sum(direct_conflict_fees) + extra_rbf_fee`, and separately evict descendants as part of the replacement process without requiring the replacer to fund them, or
3. **Document the race and advise callers** to add a safety margin above `min_replace_fee` to absorb injected children (partial mitigation only). [7](#0-6) 

### Proof of Concept

1. Alice submits `T1` (fee = F1) to the pool. Bob queries `get_transaction(T1, verbosity=2)` and reads `min_replace_fee = F1 + extra` (e.g., `0x5f5e26b` as shown in the test).
2. Bob constructs replacement `T2` spending the same inputs as `T1` with fee = `min_replace_fee + 1`.
3. Before Bob broadcasts `T2`, Alice submits 99 child transactions `C1…C99`, each spending the output of the previous, each paying the minimum fee (~242 shannons each). Total injected fee ≈ 24,000 shannons.
4. Bob submits `T2`. `check_rbf` recomputes `all_conflicted = {T1, C1…C99}` and `min_replace_fee = F1 + sum(C1..C99 fees) + extra`, which now exceeds `T2`'s fee.
5. `check_rbf` returns `Reject::RBFRejected("Tx's current fee is X, expect it to >= Y to replace old txs")` and `T2` is rejected.
6. Alice repeats step 3 whenever Bob raises his fee, keeping `T1` permanently unreplaceable at negligible cost. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L86-99)
```rust
    pub fn min_replace_fee(&self, tx: &TxEntry) -> Option<Capacity> {
        if !self.enable_rbf() {
            return None;
        }

        let mut conflicts = vec![self.get_pool_entry(&tx.proposal_short_id()).unwrap()];
        let descendants = self.pool_map.calc_descendants(&tx.proposal_short_id());
        let descendants = descendants
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        conflicts.extend(descendants);
        self.calculate_min_replace_fee(&conflicts, tx.size)
    }
```

**File:** tx-pool/src/pool.rs (L101-127)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
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
        if let Ok(res) = res {
            Some(res)
        } else {
            let fees = conflicts.iter().map(|c| c.inner.fee).collect::<Vec<_>>();
            error!(
                "conflicts: {:?} replaced_sum_fee {:?} overflow by add {}",
                conflicts.iter().map(|e| e.id.clone()).collect::<Vec<_>>(),
                fees,
                extra_rbf_fee
            );
            None
        }
    }
```

**File:** tx-pool/src/pool.rs (L611-676)
```rust
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
```

**File:** util/jsonrpc-types/src/blockchain.rs (L582-585)
```rust
    pub fee: Option<Capacity>,
    /// The minimal fee required to replace this transaction
    pub min_replace_fee: Option<Capacity>,
}
```

**File:** resource/ckb.toml (L213-214)
```text
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```
