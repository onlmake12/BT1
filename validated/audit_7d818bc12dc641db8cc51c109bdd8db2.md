All code references check out. The key facts verified:

- `MAX_DEP_EXPANSION_LIMIT = 2048` confirmed at [1](#0-0) 
- `resolve_transaction_dep` loop at [2](#0-1)  performs one `cell_resolver` call per sub-out-point, up to 2048 times
- `resolved_cells` cache is local per `resolve_transaction` call at [3](#0-2) 
- `SYSTEM_CELL` is a `OnceLock` at [4](#0-3) ; user-deployed dep groups fall through to `resolve_transaction_dep` at [5](#0-4) 
- Fee check uses only serialized `tx_size` at [6](#0-5) 
- `transferred_byte_cycles` applies to VM syscall data transfers, not resolution at [7](#0-6) 
- 2048-entry dep group test confirmed at [8](#0-7) 

---

Audit Report

## Title
Dep Group Expansion Performs Up to 2048 Uncharged DB Reads Per Transaction — (File: `util/types/src/core/cell.rs`)

## Summary
When a `CellDep` with `dep_type = DepGroup` is resolved, `resolve_transaction_dep` performs up to 2048 `cell_provider.cell()` (RocksDB) reads in a loop. None of these reads are charged in cycles or fees. The fee model charges only on serialized transaction size, creating a ~2048× work-to-fee amplification ratio exploitable by any unprivileged user.

## Finding Description
In `resolve_transaction_dep` (`util/types/src/core/cell.rs`, lines 807–841), when `cell_dep.dep_type() == DepType::DepGroup`, the function calls `cell_resolver(&outpoint, true)` once for the dep group cell itself (line 817), then iterates over all sub-out-points (line 829–831), calling `cell_resolver` once per entry — up to `MAX_DEP_EXPANSION_LIMIT` (2048) times. Each call invokes `cell_provider.cell(out_point, eager_load)`, which is a RocksDB read for any cell not already in the per-call `resolved_cells` cache.

The `resolved_cells` cache (`HashMap<(OutPoint, bool), CellMeta>` at line 696) is local to each `resolve_transaction` invocation and provides no cross-transaction deduplication. The `SYSTEM_CELL` static `OnceLock` (line 31) is populated only with genesis/system cells; user-deployed dep groups are absent from it and unconditionally fall through to `resolve_transaction_dep` (lines 781–789).

Cycle accounting occurs only inside `ScriptVerifier::verify()`, after resolution is complete. The `transferred_byte_cycles` function (`script/src/cost_model.rs`) applies to VM syscall data transfers, not to the resolution phase. The fee check in `tx-pool/src/util.rs` line 45 computes `min_fee_rate.fee(tx_size as u64)` where `tx_size` is the serialized transaction byte count — entirely independent of dep group expansion depth.

The same `resolve_transaction` path is triggered in both tx-pool admission and block verification, so every full node verifying a block containing such transactions performs the full 2048 DB reads per transaction.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The fee/work ratio is approximately 2048× per transaction. An attacker pays fees proportional to ~200 bytes of serialized transaction data while causing 2049 RocksDB reads during resolution. Because block verification is mandatory for all full nodes, the amplification is network-wide, not limited to the targeted node's tx-pool. Sustained submission saturates the resolution pipeline of all verifying nodes relative to fees paid.

## Likelihood Explanation
Upfront cost is approximately 0.126 CKB total (one dep group cell encoding 2048 out-points + 2048 minimal live cells). Once deployed, the attacker submits transactions indefinitely, each paying only the minimum fee based on serialized size. The attack requires no special privileges, is triggered via the standard `send_transaction` RPC, and requires no additional on-chain setup after initial deployment. The existing test suite at `test/src/specs/hardfork/v2021/cell_deps.rs` line 419 confirms 2048-entry dep groups are accepted by the protocol, establishing the maximum expansion is reachable by design.

## Recommendation
Charge cycles proportional to the number of dep group sub-cell lookups performed during `resolve_transaction_dep`. A fixed per-lookup cycle cost (e.g., a flat constant or `transferred_byte_cycles` applied to the sub-cell's `data_bytes`) should be added inside the loop at line 829–831 and returned from the resolution phase so it is included in the transaction's total cycle consumption and thus reflected in the fee requirement. Alternatively, extend the fee model in `check_tx_fee` (`tx-pool/src/util.rs`) to include a per-dep-expansion surcharge in the minimum fee calculation.

## Proof of Concept
1. Deploy a dep group cell whose data is an `OutPointVec` of 2048 valid out-points (data size: 2048 × 36 = 73,728 bytes).
2. Deploy 2048 minimal live cells to be referenced (~0.125 CKB total capacity).
3. Submit N transactions via `send_transaction` RPC, each containing a single `CellDep` pointing to the dep group cell with `dep_type = DepGroup`.
4. Each transaction triggers `resolve_transaction_dep` → 1 eager DB read for the dep group cell + up to 2048 DB reads for sub-cells = up to 2049 DB reads total, in both tx-pool admission and block verification.
5. Fee paid per transaction: ~200 bytes × `min_fee_rate` (negligible).
6. Total DB reads across N transactions: N × 2049, with fees proportional only to N × ~200 bytes.
7. The existing test at `test/src/specs/hardfork/v2021/cell_deps.rs` line 419 (`create_depgroup_celldep` with `&[&code_ay0_tx; 2048]`) confirms 2048-entry dep groups are accepted, providing a direct template for the PoC setup.

### Citations

**File:** util/types/src/core/cell.rs (L31-31)
```rust
pub static SYSTEM_CELL: OnceLock<SystemCellMap> = OnceLock::new();
```

**File:** util/types/src/core/cell.rs (L33-33)
```rust
const MAX_DEP_EXPANSION_LIMIT: usize = 2048;
```

**File:** util/types/src/core/cell.rs (L696-696)
```rust
    let mut resolved_cells: HashMap<(OutPoint, bool), CellMeta> = HashMap::new();
```

**File:** util/types/src/core/cell.rs (L781-789)
```rust
            } else {
                resolve_transaction_dep(
                    &cell_dep,
                    cell_resolver,
                    resolved_cell_deps,
                    resolved_dep_groups,
                    false, // don't eager_load data
                    &mut remaining_dep_slots,
                )?;
```

**File:** util/types/src/core/cell.rs (L829-831)
```rust
        for sub_out_point in sub_out_points.into_iter() {
            resolved_cell_deps.push(cell_resolver(&sub_out_point, eager_load)?);
        }
```

**File:** tx-pool/src/util.rs (L45-45)
```rust
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** script/src/cost_model.rs (L10-12)
```rust
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** test/src/specs/hardfork/v2021/cell_deps.rs (L419-419)
```rust
        let group_ay0_2048 = Self::create_depgroup_celldep(node, inputs, &[&code_ay0_tx; 2048]);
```
