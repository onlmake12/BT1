Audit Report

## Title
Dep Group Expansion Performs Up to 2048 Uncharged DB Reads Per Transaction — (File: `util/types/src/core/cell.rs`)

## Summary

CKB's dep group feature allows a single `CellDep` entry to expand into up to 2048 sub-cell database lookups during `resolve_transaction_dep`. None of these lookups are charged in cycles or fees; the fee model charges only on serialized transaction size. An attacker can deploy a 2048-entry dep group once at low cost and then submit arbitrarily many transactions, each causing 2048 DB reads during both tx-pool admission and block verification, while paying fees proportional only to ~200 bytes of serialized transaction data.

## Finding Description

In `resolve_transaction_dep` (`util/types/src/core/cell.rs`, lines 807–841), when `cell_dep.dep_type() == DepType::DepGroup`, the function calls `cell_resolver` once for the dep group cell (eager load) and then once per sub-out-point in a loop — up to `MAX_DEP_EXPANSION_LIMIT` (2048) times. Each `cell_resolver` call invokes `cell_provider.cell(out_point, eager_load)`, which is a RocksDB read for cells not already in the per-call `resolved_cells` cache.

The `resolved_cells` cache (`HashMap<(OutPoint, bool), CellMeta>` at line 696) is local to each `resolve_transaction` call. It deduplicates reads within a single transaction resolution but provides no cross-transaction caching. The `SYSTEM_CELL` static cache (`OnceLock<SystemCellMap>` at line 31) is initialized once from the genesis block's system cells only; user-deployed dep groups are not present in it and fall through to the `resolve_transaction_dep` path unconditionally (lines 781–801).

The cycle counter is incremented only inside `ScriptVerifier::verify()`, which runs after resolution is complete. The `transferred_byte_cycles` function in `script/src/cost_model.rs` applies to data read by scripts via syscalls, not to the resolution phase. The fee check in `tx-pool/src/util.rs` (`check_tx_fee`, line 45) computes the minimum fee as `min_fee_rate.fee(tx_size as u64)`, where `tx_size` is the serialized transaction size — not the number of dep group expansions or DB reads performed.

The same `resolve_transaction` path is triggered in both the tx-pool admission path and the block verification path, meaning every node that verifies a block containing such transactions also performs the 2048 DB reads per transaction.

## Impact Explanation

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The fee/work gap is approximately 2048× per transaction. An attacker pays fees proportional to ~200 bytes of serialized transaction data while causing 2048 RocksDB reads during resolution. Because block verification is mandatory for all full nodes, the amplification affects the entire network, not just the tx-pool of the targeted node. Sustained submission of such transactions can saturate the resolution pipeline of all verifying nodes relative to the fees paid.

## Likelihood Explanation

The upfront cost is approximately 0.126 CKB total (one dep group cell with 2048 out-points and 2048 minimal live cells). Once deployed, the attacker submits transactions indefinitely, each paying only a small fee based on serialized size. The attack is triggered via the standard `send_transaction` RPC, requires no special privileges, and is repeatable without additional on-chain setup. The test suite confirms 2048-entry dep groups are accepted and 2049-entry dep groups are rejected, establishing that the maximum expansion is reachable by design.

## Recommendation

Charge cycles proportional to the number of dep group sub-cell lookups performed during `resolve_transaction_dep`. A fixed per-lookup cycle cost (e.g., a flat constant or `transferred_byte_cycles` applied to the sub-cell's `data_bytes`) should be added inside the loop at line 829 and returned from the resolution phase so it can be included in the transaction's total cycle consumption and thus reflected in the fee requirement. Alternatively, extend the fee model to include a per-dep-expansion surcharge in the minimum fee calculation in `check_tx_fee`.

## Proof of Concept

1. Deploy a dep group cell whose data is an `OutPointVec` of 2048 valid out-points (data size: 2048 × 36 = 73,728 bytes; capacity cost: ~0.00074 CKB).
2. Deploy 2048 minimal live cells to be referenced (capacity cost: ~0.125 CKB total).
3. Submit N transactions via `send_transaction` RPC, each containing a single `CellDep` pointing to the dep group cell with `dep_type = DepGroup`.
4. Each transaction triggers `resolve_transaction_dep` → 1 eager DB read for the dep group cell + 2048 DB reads for sub-cells = 2049 DB reads total, in both tx-pool admission and block verification.
5. Fee paid per transaction: ~200 bytes × `min_fee_rate` (negligible).
6. Total DB reads across N transactions: N × 2049, with fees proportional only to N × 200 bytes.
7. The existing test at `test/src/specs/hardfork/v2021/cell_deps.rs` line 419 (`create_depgroup_celldep` with `&[&code_ay0_tx; 2048]`) confirms 2048-entry dep groups are accepted, providing a direct template for the PoC setup.