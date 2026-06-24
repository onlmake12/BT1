Audit Report

## Title
Unbounded Descendant Traversal in `remove_entry` Enables O(N) CPU Stall on Block Commit — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
The CKB tx-pool enforces `max_ancestors_count` (default 1,000) on insertion but has no symmetric `max_descendants_count` limit. An unprivileged attacker can submit N transactions each referencing a single in-pool transaction as a `cell_dep`, accumulating N children in `TxLinksMap` with no enforcement. When that root transaction is committed in a block, `remove_entry` unconditionally calls `update_descendants_index_key`, which performs an unbounded BFS over all N descendants and issues N `modify_by_id` calls on the pool's multi-index map, stalling the tx-pool service thread proportionally to N.

## Finding Description

**`remove_entry` calls `update_descendants_index_key` unconditionally:**
`remove_entry` at L235–250 always calls both `update_ancestors_index_key` and `update_descendants_index_key` before clearing links. There is no short-circuit when the descendant set is large.

**`update_descendants_index_key` performs an unbounded BFS:**
At L447–460, `calc_descendants` is called, which delegates to `calc_relation_ids` (links.rs L52–72) — an iterative BFS with no depth or count cap. Every descendant then receives a `modify_by_id` call.

**`cell_dep` references create parent-child links in `TxLinksMap`:**
In `get_tx_ancestors` (pool_map.rs L541–547), any in-pool transaction referenced as a `cell_dep` is added to `parents`. `_record_ancestors` (L570–572) then calls `self.links.add_child(parent, short_id)`, making the root a parent of every such child. This is confirmed by the integration test at `test/src/specs/tx_pool/limit.rs` L90–101, which explicitly submits 2,000 transactions all referencing the same `cell_dep` parent and asserts all are accepted.

**No `max_descendants_count` exists anywhere in the codebase** — confirmed by code search returning zero matches for `max_descendants`.

**`remove_entry_and_descendants` does not apply to committed transactions:**
At L252–265, `remove_entry_and_descendants` pre-clears all links before calling `remove_entry`, making the inner traversal O(1). Committed transactions must keep their descendants in the pool, so they go through the plain `remove_entry` path, which does not pre-clear links.

**Exploit path:**
1. Attacker submits `tx_root` (any valid transaction with at least one output).
2. Attacker submits N transactions `tx_1…tx_N`, each with independent inputs and `tx_root`'s output as a `cell_dep`. Each has `ancestors_count = 2`, well within the 1,000 limit.
3. `tx_root` accumulates N children in `TxLinksMap` with no enforcement.
4. A block is produced containing `tx_root`.
5. The node calls `remove_entry(&tx_root_id)` during block-commit processing.
6. `update_descendants_index_key` traverses all N descendants and calls `modify_by_id` for each — O(N) work on the pool's multi-index map, stalling the tx-pool service thread.
7. Attacker repeats with a new `tx_root`.

## Impact Explanation
The tx-pool service thread is single-threaded. An O(N) stall during block-commit processing delays block-template generation, transaction relay, and all subsequent pool operations. With the default 180 MB pool and minimal transaction sizes (~100–200 bytes), N can reach tens of thousands. A sustained attack (attacker continuously refills descendants after each block) keeps the tx-pool thread near 100% CPU, degrading block propagation latency and potentially causing the node to fall behind the chain tip. This constitutes a bad design that can cause CKB network congestion with relatively low costs, matching the **High (10001–15000 points)** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The attacker requires no privileged access — only the ability to submit valid fee-paying transactions via RPC or P2P relay. The minimum fee rate is configurable and can be very low, making the cost of filling the pool with fan-out descendants minimal. The attack is repeatable: after each block commit, the attacker submits a new root and refills descendants. The integration test `TxPoolLimitAncestorCount` already demonstrates 2,000 such transactions being accepted in a single test run, confirming the precondition is trivially satisfiable.

## Recommendation
1. **Add a `max_descendants_count` limit** symmetric to `max_ancestors_count`. Enforce it in `record_entry_descendants` when a new child is linked to an existing parent — reject the child if the parent's transitive descendant count would exceed the limit.
2. **Alternatively**, cap `edges.deps[out_point].len()` to bound the number of in-pool transactions that may reference the same `out_point` as a `cell_dep`.
3. **Short-term mitigation**: increase `min_fee_rate` to raise the cost of filling the pool with fan-out descendants, or reduce `max_tx_pool_size`.

## Proof of Concept
```
1. Submit tx_root via send_transaction RPC (any valid tx, fee >= min_fee_rate).
2. For i in 1..N:
     Submit tx_i with:
       - inputs: [some unrelated live cell_i]
       - cell_deps: [OutPoint { tx_hash: tx_root.hash(), index: 0 }]
     Each tx_i has ancestors_count = 2, accepted by the pool.
3. Assert tx_root has N children in TxLinksMap (no enforcement prevents this).
4. Mine a block containing tx_root (or submit it via a test harness).
5. Node calls remove_entry(tx_root_id):
     → update_descendants_index_key calls calc_descendants → BFS over N entries
     → N modify_by_id calls on the multi-index map
6. Measure tx-pool thread CPU time; observe O(N) scaling.
7. Repeat from step 1 with a new tx_root.

Existing integration test (test/src/specs/tx_pool/limit.rs L93-100) already
demonstrates N=2000 cell_dep children being accepted — extend it to measure
remove_entry latency to reproduce the stall.
```