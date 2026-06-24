The code confirms the claim. All referenced lines match exactly:

- [1](#0-0)  — The loop accumulates `replace_count` per-conflict with no cross-conflict deduplication.
- [2](#0-1)  — `calc_descendants` returns a per-root `HashSet`, not a globally deduplicated set.
- [3](#0-2)  — `TxLinks` has `parents: HashSet<ProposalShortId>`, confirming a transaction can have multiple parents (diamond topology is valid).
- [4](#0-3)  — `calculate_min_replace_fee` already deduplicates via a `HashMap` keyed on `id`, confirming the fix pattern is known and used elsewhere.

---

Audit Report

## Title
RBF `replace_count` Double-Counts Shared Descendants in Diamond Dependency Graphs, Causing False Rejection of Legitimate RBF Transactions - (File: `tx-pool/src/pool.rs`)

## Summary
In `check_rbf`, the `replace_count` variable is incremented by `descendants.len() + 1` for each conflicting transaction without deduplicating descendants shared across multiple conflicts. When two conflicting transactions share common descendants (a diamond-shaped dependency graph), those descendants are counted multiple times, inflating `replace_count` beyond the true unique replacement count and triggering a premature `MAX_REPLACEMENT_CANDIDATES` rejection for a legitimate RBF transaction.

## Finding Description
At `tx-pool/src/pool.rs` lines 613–624, the Rule #5 enforcement loop accumulates `replace_count` per-conflict without a global deduplication set:

```rust
let mut replace_count: usize = 0;
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;   // no cross-conflict dedup
    if replace_count > MAX_REPLACEMENT_CANDIDATES {
        return Err(Reject::RBFRejected(...));
    }
    ...
}
```

`calc_descendants` (`tx-pool/src/component/links.rs` lines 82–84) returns a `HashSet<ProposalShortId>` scoped to a single root via a BFS/DFS traversal. If conflict A has descendants `{C, D}` and conflict B has descendants `{C, E}`, the loop computes `replace_count = (2+1) + (2+1) = 6`, while the true unique replacement set `{A, B, C, D, E}` has size 5. Shared descendant C is counted twice.

The diamond dependency topology is fully supported: `TxLinks` (`tx-pool/src/component/links.rs` lines 4–8) stores `parents: HashSet<ProposalShortId>` and `children: HashSet<ProposalShortId>`, meaning a transaction C may legitimately record both A and B as parents. The `Edges` struct (`tx-pool/src/component/edges.rs` lines 8–14) maps each `OutPoint` to exactly one spending `ProposalShortId` via `inputs: HashMap<OutPoint, ProposalShortId>`, but transaction C may have two inputs — one spending an output of A and one spending an output of B — making C a recorded child of both A and B.

By contrast, `calculate_min_replace_fee` (`tx-pool/src/pool.rs` lines 104–108) already handles this correctly by deduplicating via a `HashMap` keyed on `id`, but the `replace_count` path has no equivalent guard.

## Impact Explanation
An unprivileged submitter can cause a legitimate RBF transaction to be rejected via the standard RPC with `RBFRejected("Tx conflict with too many txs, conflict txs count: N, expect <= 100")` even when the true unique replacement count is within the 100-transaction limit. This is an incorrect local RPC API rejection — the node's RBF acceptance logic produces a wrong result for a valid input. This matches the **Note (0–500 points): Any local RPC API crash** impact class (unexpected RPC-level failure triggered by a normal user action).

## Likelihood Explanation
The attack requires only standard RPC access. The attacker submits two transactions A and B (each spending a different UTXO), then submits 50 child transactions each spending one output from A and one from B. No privileged access, no majority hashpower, and no external dependency is required. The pool topology (diamond dependency) arises organically from consolidation transactions and is fully valid.

## Recommendation
Accumulate all descendants into a single global `HashSet` before computing `replace_count`, mirroring the deduplication already present in `calculate_min_replace_fee`:

```rust
let mut all_descendants: HashSet<ProposalShortId> = HashSet::new();
for conflict in conflicts.iter() {
    all_descendants.extend(self.pool_map.calc_descendants(&conflict.id));
}
let replace_count = all_descendants.len() + conflicts.len();
if replace_count > MAX_REPLACEMENT_CANDIDATES {
    return Err(Reject::RBFRejected(format!(
        "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
        replace_count, MAX_REPLACEMENT_CANDIDATES,
    )));
}
```

## Proof of Concept
1. Submit tx A spending cell X (output `out_A`).
2. Submit tx B spending cell Y (output `out_B`).
3. Submit 50 child transactions C₁…C₅₀, each spending one output from A and one from B.
4. Submit RBF tx R spending both X and Y (conflicting with A and B), with a fee exceeding the minimum.
5. In `check_rbf`: `conflicts = [A, B]`. For A: `descendants = {C₁…C₅₀}` (50 entries), `replace_count += 51`. For B: `descendants = {C₁…C₅₀}` (same 50 entries), `replace_count += 51`. Total `replace_count = 102 > 100`.
6. R is rejected with `"Tx conflict with too many txs, conflict txs count: 102, expect <= 100"` even though only 52 unique transactions (`A, B, C₁…C₅₀`) would actually be replaced.

### Citations

**File:** tx-pool/src/pool.rs (L104-108)
```rust
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
```

**File:** tx-pool/src/pool.rs (L613-624)
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
```

**File:** tx-pool/src/component/links.rs (L4-8)
```rust
#[derive(Default, Debug, Clone)]
pub struct TxLinks {
    pub parents: HashSet<ProposalShortId>,
    pub children: HashSet<ProposalShortId>,
}
```

**File:** tx-pool/src/component/links.rs (L82-84)
```rust
    pub fn calc_descendants(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.calc_relative_ids(short_id, Relation::Children)
    }
```
