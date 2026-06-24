Audit Report

## Title
RBF `replace_count` Overcounts Shared Descendants, Causing False Rejection of Legitimate Replacements - (File: `tx-pool/src/pool.rs`)

## Summary
In `TxPool::check_rbf`, the `replace_count` guard for Rule #5 (`MAX_REPLACEMENT_CANDIDATES = 100`) accumulates raw per-conflict descendant-set sizes in a loop without deduplicating descendants shared across multiple conflicts. When two or more conflicting transactions share a common descendant sub-graph, `replace_count` exceeds the true unique eviction count, causing the node to reject a legitimate RBF attempt that would only evict ≤ 100 unique transactions. An unprivileged attacker can exploit this to permanently pin a victim's transaction in the mempool.

## Finding Description
`check_rbf` at `tx-pool/src/pool.rs` lines 613–624 iterates over every direct conflict and adds `descendants.len() + 1` to `replace_count`:

```rust
let mut replace_count: usize = 0;
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;   // ← raw per-conflict, no dedup
    if replace_count > MAX_REPLACEMENT_CANDIDATES {
        return Err(Reject::RBFRejected(...));
    }
    ...
}
```

`calc_descendants` returns an independent `HashSet<ProposalShortId>` for each conflict. If two conflicts A and D share a descendant sub-graph (e.g., B spends outputs of both A and D, and B has children C₁…C₄₉), every shared descendant is counted once per conflict that owns it. With the diamond graph described in the PoC:

- `calc_descendants(A)` = {B, C₁…C₄₉} → 50 entries → `replace_count = 51`
- `calc_descendants(D)` = {B, C₁…C₄₉} → 50 entries → `replace_count = 102`
- `102 > 100` → `Reject::RBFRejected`

Unique transactions that would actually be evicted: {A, D, B, C₁…C₄₉} = 52 — well within the limit.

The fee-calculation path (`calculate_min_replace_fee`, lines 101–115) correctly deduplicates via a `HashMap` keyed on `id`, so only the Rule #5 guard is broken. The constant `MAX_REPLACEMENT_CANDIDATES = 100` is defined at line 33.

## Impact Explanation
This is a targeted transaction-pinning denial-of-service. An attacker can block any victim's RBF replacement indefinitely at negligible cost (~52 low-fee transactions). The victim cannot unstick their transaction via RBF while the attacker's diamond graph persists in the mempool. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points), as the attack is cheap, repeatable, and can be applied to any RBF user simultaneously.

## Likelihood Explanation
Any unprivileged mempool submitter can execute this attack. Required steps: (1) observe which inputs the victim's original transaction spends (public mempool data); (2) submit a diamond-shaped dependency graph rooted at two transactions each conflicting with one of the victim's inputs; (3) the attack persists until the attacker's transactions are mined or evicted. No privileged access, no key material, and no majority hashpower is required. The entry path is the standard `send_transaction` RPC or P2P relay. The attack is cheap and repeatable.

## Recommendation
Replace the per-conflict accumulation with a single deduplicated set across all conflicts, then check its cardinality once after the loop:

```rust
let mut all_to_replace: HashSet<ProposalShortId> = HashSet::new();
for conflict in conflicts.iter() {
    all_to_replace.insert(conflict.id.clone());
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    // existing ancestor-disjoint and input checks remain here
    all_to_replace.extend(descendants);
}
if all_to_replace.len() > MAX_REPLACEMENT_CANDIDATES {
    return Err(Reject::RBFRejected(format!(
        "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
        all_to_replace.len(), MAX_REPLACEMENT_CANDIDATES,
    )));
}
```

This mirrors the deduplication already applied in `calculate_min_replace_fee` and charges only unique additions.

## Proof of Concept
1. Enable RBF (`min_rbf_rate > min_fee_rate`).
2. Attacker submits: **A** (spends confirmed UTXO I₁), **D** (spends confirmed UTXO I₂), **B** (spends output of A and output of D), **C₁…C₄₉** (each spends B's output).
3. Victim submits replacement **N** spending I₁ and I₂ with a higher fee.
4. `check_rbf` finds `conflicts = [A, D]`.
   - Iteration 1 (A): `descendants = {B, C₁…C₄₉}` (50 items), `replace_count = 51`
   - Iteration 2 (D): `descendants = {B, C₁…C₄₉}` (50 items), `replace_count = 102`
   - `102 > 100` → `Reject::RBFRejected("Tx conflict with too many txs…")`
5. Unique transactions that would be evicted: {A, D, B, C₁…C₄₉} = 52 — within the limit.
6. Victim's replacement is permanently blocked.

A unit test can be written in `tx-pool` that constructs this exact pool state and asserts that `check_rbf` incorrectly returns `Err` for a replacement that should succeed.