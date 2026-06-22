### Title
RBF `min_replace_fee` Inflation via High-Fee Descendants Pins Replacement — (`tx-pool/src/pool.rs`)

---

### Summary

The `check_rbf` function in `tx-pool/src/pool.rs` computes the minimum fee a replacement transaction must pay as the **live sum of all conflicted transactions' fees plus all their descendants' fees**, with no cap on the total fee sum. An attacker who controls the original conflicting transaction can add up to 99 high-fee descendants before a victim attempts replacement, inflating `min_replace_fee` to an arbitrarily large value and permanently blocking the victim's replacement transaction.

---

### Finding Description

`calculate_min_replace_fee` computes:

```
min_replace_fee = sum(fee of tx_to_replace + all descendants) + extra_rbf_fee
``` [1](#0-0) 

`check_rbf` builds `all_conflicted` by appending every descendant of every conflicting transaction: [2](#0-1) 

Then it enforces the fee check against this live, attacker-inflatable sum: [3](#0-2) 

The only guard is `MAX_REPLACEMENT_CANDIDATES = 100`, which caps the **count** of descendants but places no cap on the **total fee sum** those descendants carry: [4](#0-3) 

**Structural mismatch (direct analog to the report):**
- The victim's replacement fee is fixed at the time they construct `tx_B`.
- The threshold (`min_replace_fee`) is computed from the **current live pool state**, which the attacker controls by adding descendants after `tx_A` is accepted.

This is the exact same temporal mismatch as the Alchemix report: the user's "power" is measured at a past point; the threshold is computed from a live value the adversary can inflate.

---

### Impact Explanation

An attacker who submitted `tx_A` spending input `X` can add up to 99 descendants of `tx_A`, each carrying a large fee (e.g., 1,000,000 shannons each). The resulting `min_replace_fee` becomes:

```
F_A + 99 × 1,000,000 + extra_rbf_fee ≈ 99,000,000+ shannons
```

Any victim attempting to replace `tx_A` with `tx_B` (also spending `X`) must pay more than this inflated threshold. Because the victim's fee is fixed in their already-constructed transaction, their replacement is rejected with:

```
"Tx's current fee is X, expect it to >= Y to replace old txs"
```

This allows the attacker to **permanently pin** `tx_A` in the pool for the cost of the descendant fees they themselves pay — a griefing attack where the attacker forces the victim to pay more than the attacker spent. For time-sensitive transactions (e.g., those racing against a `since`-locked cell expiry), this denial of replacement has direct protocol-level consequences.

Impact matches the bounty scope: **manipulation of tx-pool admission deviating from the intended RBF outcome**, reachable by any unprivileged `send_transaction` RPC caller or P2P transaction relayer.

---

### Likelihood Explanation

- RBF is enabled on mainnet when `min_rbf_rate > min_fee_rate` (default config: 1500 vs 1000 shannons/KB).
- The attacker needs only to be the sender of the original conflicting transaction and have enough CKB to fund up to 99 descendants.
- No special privilege, key, or majority hashpower is required.
- The attack is fully executable via the public `send_transaction` RPC endpoint.
- The victim has no recourse once the descendants are in the pool, because Rule #5 (`MAX_REPLACEMENT_CANDIDATES`) will also reject the victim's replacement if the attacker has already added ≥100 descendants. [5](#0-4) 

---

### Recommendation

Cap the **total fee sum** used in `calculate_min_replace_fee`, not just the count of descendants. Concretely:

1. Introduce a `max_replacement_fee_sum` consensus/config parameter.
2. In `calculate_min_replace_fee`, clamp `replaced_sum_fee` to this cap before adding `extra_rbf_fee`.
3. Alternatively, exclude descendants' fees from the replacement threshold and only require the new transaction to exceed the **direct conflict's fee** plus `extra_rbf_fee` — matching Bitcoin's BIP-125 Rule #3 intent more faithfully. [6](#0-5) 

---

### Proof of Concept

```
1. Node has RBF enabled (min_rbf_rate=1500 > min_fee_rate=1000).

2. Attacker submits tx_A:
   - Spends cell X (owned by attacker)
   - Fee: 1,000 shannons

3. Victim constructs tx_B:
   - Also spends cell X (double-spend / replacement intent)
   - Fee: 500,000 shannons  (well above min_fee_rate)

4. Before victim submits tx_B, attacker submits 99 descendants of tx_A
   (tx_A_child_1 … tx_A_child_99), each with fee = 1,000,000 shannons.
   All 99 are accepted (replace_count = 100, exactly at the limit).

5. Victim submits tx_B via send_transaction RPC.

6. check_rbf computes:
   all_conflicted = [tx_A, tx_A_child_1, …, tx_A_child_99]  (100 entries)
   min_replace_fee = 1,000 + 99×1,000,000 + extra_rbf_fee
                   ≈ 99,001,000+ shannons

7. tx_B.fee (500,000) < min_replace_fee (99,001,000).
   Node returns: RBFRejected("Tx's current fee is 500000,
                  expect it to >= 99001000 to replace old txs")

8. tx_A (attacker's original) remains pinned in the pool.
   Victim cannot replace it without paying ~99× what the attacker spent.
``` [7](#0-6)

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
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
