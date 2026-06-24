Audit Report

## Title
VerifyQueue Admits Conflicting Input-Cell Transactions, Forcing Redundant CKB-VM Verification Work — (`tx-pool/src/process.rs`, `tx-pool/src/pool_cell.rs`)

## Summary
CKB's tx-pool deduplicates verify-queue entries by `ProposalShortId` (a hash of the transaction itself), not by input `OutPoint`. Two distinct transactions spending the same live cell therefore both pass the enqueue gate, both pass `pre_check` under a shared read lock, and both undergo full CKB-VM script execution. The conflict is detected only at `submit_entry` under the write lock, after all verification work has already been performed. An unprivileged attacker can exploit this to force the node to perform arbitrarily multiplied CKB-VM verification work at negligible cost.

## Finding Description

**Stage 1 — Enqueue** (`resumeble_process_tx`, `process.rs` L335–352):

`verify_queue_contains` checks `queue.contains_key(&tx.proposal_short_id())`. Two transactions spending the same cell but producing different outputs have different tx hashes and thus different `ProposalShortId`s. Both return `false` and both are enqueued via `enqueue_verify_queue`.

**Stage 2 — Pre-check** (`pre_check`, `process.rs` L269–316, under read lock):

`pre_check` calls `resolve_tx` → `resolve_tx_from_pool` → constructs a `PoolCell` overlay. `PoolCell::cell()` (`pool_cell.rs` L19–22) marks an `OutPoint` as `Dead` only when `pool_map.edges.get_input_ref(out_point).is_some()`. The `pool_map.edges.inputs` map is populated only when a transaction is successfully committed via `submit_entry` → `_submit_entry` → `add_pending/add_proposed`. Transactions sitting in the `VerifyQueue` are never inserted into `pool_map`; their inputs are never registered in `edges.inputs`. Therefore, when tx_B's `pre_check` runs concurrently with tx_A's CKB-VM verification (which holds no lock), the shared cell appears live and tx_B passes resolution cleanly.

Two workers exist (`WorkerRole::OnlySmallCycleTx` and `WorkerRole::SubmitTimeFirst`, `verify_mgr.rs` L14–17), each running `_process_tx` independently. The pipeline in `_process_tx` (`process.rs` L705–777) is:
1. `pre_check` — acquires **read** lock (allows concurrent execution)
2. `verify_rtx` — **no lock** (expensive CKB-VM execution)
3. `submit_entry` — acquires **write** lock

Both workers can be simultaneously past step 1 and executing step 2 for conflicting transactions.

**Stage 3 — Submit** (`submit_entry`, `process.rs` L96–116, under write lock):

Only here does `find_conflict_outpoint` detect the double-spend. The second transaction is rejected with `Reject::Resolve(OutPointError::Dead(...))`. All CKB-VM cycles consumed in step 2 for that transaction are wasted.

**Why existing guards are insufficient:**
- `verify_queue_contains` (`process.rs` L349): keyed on `ProposalShortId`, not on input cells.
- `PoolCell::cell()` (`pool_cell.rs` L20): only consults `pool_map.edges.inputs`, which is unpopulated for in-flight transactions.
- The 256 MB verify-queue size cap (`verify_queue.rs` L18) limits total data volume but does not prevent packing many conflicting transactions within that budget.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

CKB-VM verification is the most expensive operation in the tx-pool pipeline, bounded by `max_tx_verify_cycles` (consensus maximum). An attacker submitting N transactions all spending the same live cell forces N full CKB-VM verifications, of which N−1 are entirely wasted. Because rejected transactions are never included in a block, the attacker pays no on-chain fees for the N−1 wasted verifications. The verification worker pool is saturated, degrading throughput for legitimate transactions and potentially stalling block assembly. Targeting multiple nodes simultaneously constitutes a low-cost network-wide congestion attack.

## Likelihood Explanation
The attack requires only the ability to call `send_transaction` via RPC or relay transactions over P2P — no privileged access, no key material, no majority hashpower. Constructing N transactions spending the same confirmed cell is trivial. The attacker needs one live cell; since only the first transaction is ever confirmed, the cell is consumed once per attack round, but the attacker can maintain a supply of cells or use a chain of self-controlled UTXOs. The `min_fee_rate` check in `pre_check` imposes a small declared-fee cost, but the verification cost imposed on the node scales with `declared_cycles` up to `max_tx_verify_cycles`, creating a large cost asymmetry. The attack is repeatable and requires no special timing beyond submitting transactions faster than the verify workers drain the queue.

## Recommendation
Track in-flight input cells in the `VerifyQueue` (or a parallel `HashSet<OutPoint>`). In `resumeble_process_tx`, before calling `enqueue_verify_queue`, iterate the new transaction's inputs and check whether any `OutPoint` is already claimed by a transaction currently in the verify queue. If so, either reject the new transaction immediately (one in-flight claimant per cell at a time) or apply the same RBF rules used at `submit_entry`. The check must be performed atomically with the enqueue operation (i.e., under the verify-queue write lock) to avoid a TOCTOU race. This mirrors the standard mitigation: "ensure only one swap can be in-flight at a time."

## Proof of Concept
1. Identify a live confirmed cell `C` on the network controlled by the attacker.
2. Construct tx_A and tx_B, both spending `C`, with different outputs (different tx hashes / `ProposalShortId`s). Set declared cycle count to `max_tx_verify_cycles` for both.
3. Submit tx_A via `send_transaction` RPC → `verify_queue_contains` returns `false`; tx_A enters the verify queue.
4. Immediately submit tx_B via `send_transaction` RPC → `verify_queue_contains` returns `false` (different `ProposalShortId`); tx_B also enters the verify queue.
5. Both transactions are picked up by the two verify workers (`OnlySmallCycleTx` and `SubmitTimeFirst`). Both pass `pre_check` under the read lock (cell `C` is not in `pool_map.edges.inputs`). Both proceed to full CKB-VM execution.
6. One transaction (e.g., tx_A) completes first and succeeds at `submit_entry`; cell `C` is now registered in `pool_map.edges.inputs`.
7. The other transaction (tx_B) completes CKB-VM execution but fails at `submit_entry` with `OutPointError::Dead` — all its verification cycles are wasted.
8. Repeat with N transactions to multiply wasted work by N, bounded only by the 256 MB verify-queue size limit.