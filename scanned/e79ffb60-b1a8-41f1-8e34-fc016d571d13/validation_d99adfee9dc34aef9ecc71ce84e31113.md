Audit Report

## Title
Stale `ancestors_size` Guard in `txs_to_commit` Causes Valid Transactions to Be Excluded from Block Templates — (`File: tx-pool/src/component/tx_selector.rs`)

## Summary

In `TxSelector::txs_to_commit`, the per-iteration size/cycles guard computes `next_size = size + tx_entry.ancestors_size`, where `tx_entry.ancestors_size` is the total size of the transaction plus all its ancestors. However, `size` is a running accumulator that already includes ancestors added in prior iterations. When `update_modified_entries` fails to move a descendant into `modified_entries` — a known inconsistency explicitly acknowledged in the source — that descendant remains in `proposed_pool` with a stale `ancestors_size` that double-counts already-committed ancestors. The guard then rejects the transaction even though its actual incremental contribution would fit within the block limits.

## Finding Description

**Guard (lines 148–162):**
```rust
let next_size   = size.saturating_add(tx_entry.ancestors_size);
let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);
if next_cycles > cycles_limit || next_size > size_limit {
    consecutive_failed += 1;
    ...
    continue;
}
```
`tx_entry.ancestors_size` is the total size of the transaction plus every ancestor. `size` is a running total that already includes ancestors added in previous iterations.

**Actual addition (lines 207–218):**
```rust
for (short_id, entry) in &ancestors {
    let is_new = self.fetched_txs.insert(short_id.clone());
    if !is_new { continue; }   // already-added ancestors are skipped
    size = size.saturating_add(entry.size);
    ...
}
```
Already-fetched ancestors are skipped, so only the incremental bytes are actually added. The guard and the addition are inconsistent.

**Mitigation and its acknowledged gap (lines 241–262):**
`update_modified_entries` calls `sub_ancestor_weight` on descendants of newly-added transactions and moves them into `modified_entries` with a corrected `ancestors_size`. However, the code itself contains the comment:
```
// Note: since https://github.com/nervosnetwork/ckb/pull/3706
// calc_descendants() may not consistent
```
When `calc_descendants()` misses a descendant, that descendant stays in `proposed_pool` with its original stale `ancestors_size`. `skip_proposed_entry` only skips entries that are in `fetched_txs`, `modified_entries`, or `failed_txs` — a missed descendant is in none of these, so it is evaluated with the stale value.

**Exploit path:**
1. Tx A (size=100) is in the proposed pool.
2. Tx B (size=100, ancestor={A}, `ancestors_size`=200, higher fee-rate) is selected first. A and B are added; `size=200`, `fetched_txs={A,B}`.
3. `update_modified_entries` is called. If `calc_descendants(A)` misses Tx C (a sibling of B, also with ancestor={A}), Tx C remains in `proposed_pool` with stale `ancestors_size=200`.
4. Tx C is selected next. Guard: `next_size = 200 + 200 = 400 > size_limit=350` → rejected.
5. Actual incremental bytes: only C.size=100 (A is already in `fetched_txs`). Actual `next_size = 300 ≤ 350`. Tx C is incorrectly excluded.

## Impact Explanation

The bug causes valid transactions to be silently dropped from block templates, reducing miner fee revenue and delaying confirmation for affected senders. This is a suboptimal block-template selection outcome that constitutes an important performance deficiency in CKB's transaction packaging pipeline. This matches the allowed bounty impact: **Low (501–2000 points): Any other important performance improvements for CKB.**

The impact does not rise to Medium or higher: no funds are at risk, no consensus deviation occurs, and no node crash results.

## Likelihood Explanation

The precondition — two transactions sharing a common ancestor (CPFP pattern) — is routine on mainnet. The additional condition — `calc_descendants()` missing one of them — is explicitly acknowledged in the source as a known inconsistency introduced by PR #3706. The scenario is therefore realistic and reproducible by any transaction sender who submits a CPFP chain under normal mempool conditions. No special privileges or attacker capabilities are required.

## Recommendation

Replace the guard's use of `tx_entry.ancestors_size` with the actual incremental cost: compute the set of ancestors not yet in `fetched_txs` before the guard, sum only their sizes and cycles, and use that sum for the limit check. This makes the guard consistent with the actual addition loop at lines 207–218. Alternatively, ensure `update_modified_entries` is always consistent — i.e., every descendant of a newly-added transaction is moved to `modified_entries` with a corrected `ancestors_size` before the next outer-loop iteration — so that no descendant is ever evaluated from `proposed_pool` with a stale value.

## Proof of Concept

**Setup (unit test outline):**
- `size_limit = 350`, `cycles_limit = ∞`
- Tx A: `size=100`, no ancestors → `ancestors_size=100`
- Tx B: `size=100`, ancestor={A} → `ancestors_size=200`, fee-rate > C
- Tx C: `size=100`, ancestor={A} → `ancestors_size=200`, fee-rate < B; Tx C is **not** a descendant of Tx B

**Steps:**
1. Add A, B, C to `proposed_pool`.
2. Simulate `calc_descendants(A)` returning only `{B}` (omitting C, as acknowledged possible since PR #3706).
3. Call `txs_to_commit(350, ∞)`.
4. **Iteration 1:** B is selected (higher fee-rate). Guard: `0+200=200≤350` → passes. A and B added; `size=200`. `update_modified_entries` called; C is missed.
5. **Iteration 2:** C is selected from `proposed_pool` with stale `ancestors_size=200`. Guard: `200+200=400>350` → **C is rejected**.
6. **Expected correct behavior:** C's incremental size is 100 (A already fetched); `200+100=300≤350` → C should be included.
7. **Assert:** `entries` contains only A and B; C is absent despite fitting within the limit. Fixing the guard to use incremental size causes C to be included.