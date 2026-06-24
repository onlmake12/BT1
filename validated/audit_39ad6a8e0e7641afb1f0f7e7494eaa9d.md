Audit Report

## Title
RBF `min_replace_fee` Inflation via High-Fee Descendants Enables Transaction Pinning — (`tx-pool/src/pool.rs`)

## Summary
The `check_rbf` function in `tx-pool/src/pool.rs` builds `all_conflicted` by appending every descendant of every conflicting transaction, then passes the full slice to `calculate_min_replace_fee`, which sums all fees with no cap. An attacker who controls the original conflicting transaction can pre-load exactly 99 high-fee descendants (the count guard allows exactly 100 total), inflating `min_replace_fee` to an arbitrarily large value and permanently blocking any victim RBF attempt at a realistic fee.

## Finding Description
`MAX_REPLACEMENT_CANDIDATES` is defined as `100` at line 33. [1](#0-0) 

`check_rbf` accumulates descendants into `all_conflicted` and guards only with `replace_count > MAX_REPLACEMENT_CANDIDATES`, meaning exactly 100 entries (tx_A + 99 descendants) pass the check. [2](#0-1) 

`calculate_min_replace_fee` sums the fees of every entry in the passed slice with no upper bound on the total. [3](#0-2) 

The resulting `min_replace_fee` is enforced against the replacement transaction's fee at lines 665–670. [4](#0-3) 

**Root cause:** `min_replace_fee` is derived from live, attacker-controlled pool state (descendant fees), while the victim's fee is fixed at construction time. The `> 100` guard (not `>= 100`) allows exactly 100 candidates through, and the fee sum of those 100 is unbounded. The attacker can preemptively add 99 high-fee descendants to tx_A immediately after it is accepted, before the victim submits tx_B. Once those descendants are in the pool, the victim's replacement is rejected regardless of fee level (short of paying the entire inflated sum).

## Impact Explanation
This is a targeted transaction pinning attack reachable by any unprivileged `send_transaction` RPC caller. For time-sensitive transactions racing against a `since`-locked cell expiry or a payment channel close deadline, permanent denial of replacement has direct protocol-level consequences including potential loss of funds. At scale — one or more attackers targeting multiple victims — this constitutes a bad design causing CKB mempool congestion with relatively few costs, matching the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
- RBF is enabled on mainnet when `min_rbf_rate > min_fee_rate` (default: 1500 vs 1000 shannons/KB), confirmed by `enable_rbf()` at line 82. [5](#0-4) 
- The attacker only needs to be the sender of the original conflicting transaction and have enough CKB to fund 99 descendants.
- No special privilege, key, or majority hashpower is required.
- The attack is fully executable via the public `send_transaction` RPC endpoint.
- The attacker can add descendants immediately after tx_A is accepted, before any victim attempts replacement.
- The victim has no recourse once ≥100 descendants exist; Rule #5 also rejects outright at that point.

## Recommendation
1. Cap the **total fee sum** used in `calculate_min_replace_fee`, not just the count of descendants. Introduce a `max_replacement_fee_sum` consensus/config parameter and clamp `replaced_sum_fee` to this cap before adding `extra_rbf_fee`.
2. Alternatively, exclude descendants' fees from the replacement threshold entirely and only require the new transaction to exceed the **direct conflict's fee** plus `extra_rbf_fee`, matching Bitcoin BIP-125 Rule #3 intent more faithfully.
3. Change the count guard from `> MAX_REPLACEMENT_CANDIDATES` to `>= MAX_REPLACEMENT_CANDIDATES` to close the off-by-one that allows exactly 100 entries through.

## Proof of Concept
```
1. Node has RBF enabled (min_rbf_rate=1500 > min_fee_rate=1000).

2. Attacker submits tx_A:
   - Spends cell X (owned by attacker)
   - Fee: 1,000 shannons

3. Attacker immediately submits 99 descendants of tx_A
   (tx_A_child_1 … tx_A_child_99), each with fee = 1,000,000 shannons.
   replace_count = 99 + 1 = 100; 100 > 100 is false → all accepted.

4. Victim constructs tx_B spending cell X, fee = 500,000 shannons,
   and submits via send_transaction RPC.

5. check_rbf computes:
   all_conflicted = [tx_A, tx_A_child_1, …, tx_A_child_99]  (100 entries)
   min_replace_fee = 1,000 + 99×1,000,000 + extra_rbf_fee ≈ 99,001,000+ shannons

6. tx_B.fee (500,000) < min_replace_fee (99,001,000).
   Node returns:
   RBFRejected("Tx's current fee is 500000, expect it to >= 99001000 to replace old txs")

7. tx_A remains pinned. Victim cannot replace it without paying ~99×
   what the attacker spent in total descendant fees.
```

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L81-83)
```rust
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```

**File:** tx-pool/src/pool.rs (L101-114)
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
```

**File:** tx-pool/src/pool.rs (L613-645)
```rust
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
```

**File:** tx-pool/src/pool.rs (L664-671)
```rust
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
```
