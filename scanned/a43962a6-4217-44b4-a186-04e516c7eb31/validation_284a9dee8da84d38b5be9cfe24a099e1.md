Audit Report

## Title
Verify-Queue Admits Conflicting Input-Cell Transactions, Enabling Multiplied CKB-VM Verification Work — (`tx-pool/src/process.rs`, `tx-pool/src/pool_cell.rs`)

## Summary

CKB's transaction admission pipeline checks for duplicate `ProposalShortId`s but not for conflicting input cells when enqueuing into the `VerifyQueue`. Because `PoolCell` only consults `pool_map.edges.inputs` — which is populated only after full verification and `submit_entry` — two or more distinct transactions spending the same live cell can simultaneously reside in the verify queue and each undergo full, expensive CKB-VM script execution. Only one will succeed at `submit_entry`; all others waste their verification cycles. An unprivileged attacker who owns a single live cell can exploit this to force the node to perform arbitrarily multiplied CKB-VM verification work at near-zero cost.

## Finding Description

**Stage 1 — Enqueue** (`resumeble_process_tx`, `process.rs` L335–352):

The duplicate check calls `verify_queue_contains`, which resolves to `queue.contains_key(&tx.proposal_short_id())`. `ProposalShortId` is derived from the transaction hash. Two distinct transactions spending the same cell have different hashes and different `ProposalShortId`s, so both pass this gate unconditionally.

**Stage 2 — Pre-check** (`pre_check`, `process.rs` L269–316):

`pre_check` calls `resolve_tx` → `resolve_tx_from_pool` (`pool.rs` L372–384), which constructs a `PoolCell` overlay. `PoolCell::cell()` (`pool_cell.rs` L19–22) marks an outpoint as `Dead` only if `pool_map.edges.get_input_ref(out_point).is_some()`. Inputs are inserted into `pool_map.edges.inputs` only during `_submit_entry`, which runs after full CKB-VM verification. Transactions sitting in the `VerifyQueue` are never in `pool_map`; their inputs are never in `pool_map.edges.inputs`. Therefore, when tx_B's `pre_check` runs while tx_A (spending the same cell) is still being verified, the cell appears live and tx_B passes resolution cleanly.

**Stage 3 — CKB-VM execution** (`_process_tx`, `process.rs` L705–777):

After `pre_check` succeeds, `verify_rtx` is called unconditionally. This is where full CKB-VM script execution occurs, bounded by `declared_cycles` (up to `max_tx_verify_cycles`). Both tx_A and tx_B reach this stage.

**Stage 4 — Submit** (`submit_entry`, `process.rs` L96–116):

Only here, under write lock, does `find_conflict_outpoint` detect the double-spend. The second transaction is rejected with `Reject::Resolve(OutPointError::Dead(...))`. All CKB-VM cycles spent on it are wasted.

The `VerifyQueue` itself (`verify_queue.rs` L198–236) has no concept of input-cell occupancy; its `add_tx` method only checks `contains_key` by `ProposalShortId`.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker submitting N transactions all spending the same live cell forces the node to run N full CKB-VM verifications, of which N−1 are entirely wasted. The verify queue accepts up to 256 MB of transaction data (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`, `verify_queue.rs` L17–18). Within that budget, an attacker can pack many conflicting high-cycle transactions. Critically, in CKB fees are implicit (inputs minus outputs); rejected transactions are never committed, so the attacker pays fees only for the one transaction that succeeds. The N−1 rejected transactions cost the attacker nothing beyond network bandwidth. This constitutes a severe cost asymmetry: the attacker pays O(1) CKB while forcing O(N × max_tx_verify_cycles) of CKB-VM work on the node, degrading throughput for legitimate transactions and potentially stalling block assembly.

## Likelihood Explanation

The attack requires only: (1) ownership of a single live confirmed cell, and (2) the ability to submit transactions via `send_transaction` RPC or P2P relay. No privileged access, key material beyond the attacker's own cell, or majority hashpower is needed. Constructing N transactions spending the same cell with different outputs (yielding different tx hashes and `ProposalShortId`s) is trivial. The `min_fee_rate` imposes a cost only on the one transaction that succeeds; all rejected transactions impose zero fee cost on the attacker. The attack is repeatable as long as the attacker controls a live cell.

## Recommendation

Track in-flight input cells in the `VerifyQueue` (or a parallel `HashSet<OutPoint>`). In `resumeble_process_tx`, before calling `enqueue_verify_queue`, iterate the new transaction's inputs and check whether any `OutPoint` is already claimed by a transaction currently in the verify queue. If so, either reject the new transaction immediately with `Reject::Resolve(OutPointError::Dead(...))` or apply the same RBF rules used at `submit_entry` time. This ensures at most one in-flight claimant per cell at any time, eliminating the wasted verification work.

## Proof of Concept

1. Identify a live confirmed cell `C` owned by the attacker (attacker holds the private key for its lock script).
2. Construct tx_A and tx_B, both spending `C` as an input, with different outputs (ensuring different tx hashes and `ProposalShortId`s). Set both to the maximum declared cycle count.
3. Submit tx_A via `send_transaction` RPC → `verify_queue_contains` returns `false` (no entry for tx_A's `ProposalShortId`); tx_A enters the verify queue.
4. Immediately submit tx_B via `send_transaction` RPC → `verify_queue_contains` returns `false` (different `ProposalShortId`); `orphan_contains` returns `false`; tx_B also enters the verify queue.
5. Both transactions are picked up by verify workers; `pre_check` passes for both because `pool_map.edges.inputs` does not yet contain cell `C`'s outpoint; full CKB-VM execution runs for both.
6. tx_A completes first and is inserted into `pool_map` via `submit_entry`; cell `C`'s outpoint is now registered in `pool_map.edges.inputs`.
7. tx_B completes CKB-VM verification but fails at `submit_entry` with `OutPointError::Dead` — all its verification cycles are wasted.
8. Repeat with N transactions to multiply the wasted work by N. Since only the one committed transaction costs fees, the attacker's CKB cost is O(1) regardless of N.