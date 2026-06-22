### Title
RBF Replacement Permanently DoS-able via Descendant Flooding — (`File: tx-pool/src/pool.rs`)

---

### Summary

The `check_rbf` function in `tx-pool/src/pool.rs` enforces a hard cap of 100 replacement candidates (Rule #5). Because the pool enforces a `max_ancestors_count` limit but has **no corresponding `max_descendants_count` limit**, an unprivileged attacker can front-run a victim's pending transaction by flooding the pool with descendant transactions. Once the descendant count exceeds 100, every subsequent RBF replacement attempt by the victim is permanently rejected with `"Tx conflict with too many txs"`, for as long as the attacker keeps those descendants alive in the pool.

---

### Finding Description

`TxPool::check_rbf` in `tx-pool/src/pool.rs` implements Bitcoin-style RBF rules. Rule #5 counts all descendants of every conflicting transaction and rejects the replacement if the total exceeds `MAX_REPLACEMENT_CANDIDATES`:

```rust
// tx-pool/src/pool.rs  lines 611-624
let mut replace_count: usize = 0;
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;
    if replace_count > MAX_REPLACEMENT_CANDIDATES {          // 100
        return Err(Reject::RBFRejected(format!(
            "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
            replace_count, MAX_REPLACEMENT_CANDIDATES,
        )));
    }
``` [1](#0-0) 

`MAX_REPLACEMENT_CANDIDATES` is a fixed constant of 100: [2](#0-1) 

`calc_descendants` performs an unbounded graph walk — there is no `max_descendants_count` field in `PoolMap`, only `max_ancestors_count`: [3](#0-2) 

The `check_and_record_ancestors` function enforces the ancestor limit on admission, but there is no symmetric enforcement for descendants: [4](#0-3) 

`check_rbf` is invoked inside the write lock in `submit_entry`, so the descendant count it observes is the live pool state at the moment of the replacement attempt: [5](#0-4) 

**Attack scenario:**

1. Victim submits `T1` (with ≥2 outputs) to the pool via RPC or P2P relay.
2. Attacker observes `T1` (via `get_raw_tx_pool` RPC or compact-block relay).
3. Attacker builds a tree of descendant transactions spending `T1`'s outputs. With 2 outputs per transaction and 7 levels of depth, the tree yields 2+4+8+16+32+64+128 = 254 descendants. Each descendant has at most 7 ancestors — well within the default `max_ancestors_count` of 25 — so every descendant is accepted by the pool.
4. Victim submits `T2` (same inputs as `T1`, higher fee) to RBF-replace `T1`.
5. `check_rbf` counts 254 descendants of `T1` → `replace_count = 255 > 100` → returns `Reject::RBFRejected("Tx conflict with too many txs …")`.
6. The victim's replacement is rejected. As long as the attacker keeps ≥100 descendants in the pool, every future replacement attempt fails identically.

The attacker's descendant transactions are valid and may eventually be confirmed if `T1` is confirmed without replacement, so the attacker bears only the minimum-fee cost of ~100 transactions. If the pool evicts some descendants (due to pool-full eviction), the attacker can cheaply resubmit them.

---

### Impact Explanation

A victim whose transaction is in the pool with RBF enabled cannot replace it with a higher-fee version for as long as the attacker maintains the descendant flood. This can:

- Prevent time-sensitive transactions (e.g., those using `since` locks near expiry) from being bumped in fee and confirmed on time.
- Force the victim's transaction to expire from the pool (`expiry_hours`) without ever being confirmed, requiring a full resubmission cycle.
- Be sustained indefinitely at low cost to the attacker.

---

### Likelihood Explanation

Medium. RBF must be enabled on the target node (`min_rbf_rate > min_fee_rate`), which is an opt-in configuration but is documented and used. The attacker needs only ~100 valid transactions with minimum fees, no privileged access, and no cryptographic material beyond their own keys. The victim's pending transaction is publicly observable via the RPC or P2P relay. There is no loss for the attacker if `T1` is eventually confirmed, since the attacker's descendants would then also be confirmed.

---

### Recommendation

Introduce a `max_descendants_count` limit in `PoolMap` (symmetric to `max_ancestors_count`) and enforce it on admission in `add_entry`. When a new transaction would push any of its ancestors' descendant counts above the limit, reject it with `Reject::ExceededMaximumDescendantsCount`. This mirrors Bitcoin Core's `limitdescendantcount` policy and makes the descendant-flooding attack infeasible.

Additionally, the `MAX_REPLACEMENT_CANDIDATES` cap in `check_rbf` should be documented as a DoS surface if no descendant cap is enforced.

---

### Proof of Concept

```
1. Node has RBF enabled (min_rbf_rate > min_fee_rate).

2. Victim submits T1 with 2 outputs (out[0], out[1]) to the pool.

3. Attacker builds a 7-level binary tree rooted at T1:
   - Level 1: C1_0 spends T1.out[0], C1_1 spends T1.out[1]          (2 txs)
   - Level 2: C2_00 spends C1_0.out[0], C2_01 spends C1_0.out[1],
              C2_10 spends C1_1.out[0], C2_11 spends C1_1.out[1]    (4 txs)
   - ...
   - Level 7: 128 txs
   Total descendants: 254. Each has ≤7 ancestors → accepted by pool.

4. Victim submits T2 (same inputs as T1, fee > T1.fee + min_rbf_fee):
   check_rbf:
     conflict_ids = {T1}
     descendants of T1 = 254
     replace_count = 254 + 1 = 255 > MAX_REPLACEMENT_CANDIDATES (100)
     → Err(RBFRejected("Tx conflict with too many txs, conflict txs count: 255, expect <= 100"))

5. T2 is rejected. Victim cannot replace T1.
   Attacker resubmits any evicted descendants to maintain the block.
```

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L611-624)
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

**File:** tx-pool/src/component/pool_map.rs (L588-601)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** tx-pool/src/process.rs (L104-106)
```rust
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
```
