All code references are accurate. Let me verify one more detail about the actual overhead of `load_cell_data_hash` and whether there are any other guards I might have missed.

Audit Report

## Title
Duplicate Sub-Out-Points in Dep Group Inflate Unmetered Node Validation Work - (File: util/types/src/core/cell.rs)

## Summary
`resolve_transaction_dep` parses an `OutPointVec` from a dep group cell's data and pushes all entries—including duplicates—into `resolved_cell_deps` without deduplication. An unprivileged attacker can craft a dep group cell containing up to `MAX_DEP_EXPANSION_LIMIT` (2048) identical out-points, causing every validating node to perform up to 2048× more unmetered CPU work in `TxData::new` per transaction, compared to a semantically equivalent single-entry dep group. The existing `DuplicateDepsVerifier` does not inspect dep group contents, and the integration test `test_duplicate_in_depgroup_type` explicitly confirms this path passes validation.

## Finding Description
In `resolve_transaction_dep` (`util/types/src/core/cell.rs`, lines 822–831), after parsing `sub_out_points` from the dep group cell's data, no deduplication is performed before decrementing `remaining_dep_slots` or pushing resolved `CellMeta` entries:

```rust
let sub_out_points =
    parse_dep_group_data(data).map_err(|_| OutPointError::InvalidDepGroup(outpoint))?;

*remaining_dep_slots = remaining_dep_slots
    .checked_sub(sub_out_points.len())
    .ok_or(OutPointError::OverMaxDepExpansionLimit)?;

for sub_out_point in sub_out_points.into_iter() {
    resolved_cell_deps.push(cell_resolver(&sub_out_point, eager_load)?);
}
```

`MAX_DEP_EXPANSION_LIMIT` is 2048 (`util/types/src/core/cell.rs`, line 33), so an attacker can fill a dep group with 2048 copies of the same valid out-point. All 2048 `CellMeta` entries are pushed into `resolved_cell_deps`.

`TxData::new` (`script/src/types.rs`, lines 701–714) then iterates every entry unconditionally:

```rust
for (i, cell_meta) in resolved_cell_deps.iter().enumerate() {
    let data_hash = data_loader.load_cell_data_hash(cell_meta).expect("cell data hash");
    let lazy = LazyData::from_cell_meta(cell_meta);
    binaries_by_data_hash.insert(data_hash.to_owned(), (i, lazy.to_owned()));
    if let Some(t) = &cell_meta.cell_output.type_().to_opt() {
        binaries_by_type_hash
            .entry(t.calc_script_hash())
            .and_modify(|bin| bin.merge(&data_hash))
            .or_insert_with(|| Binaries::new(data_hash.to_owned(), i, lazy.to_owned()));
    }
}
```

For each of the 2048 duplicate entries, the node performs: a `load_cell_data_hash` call (memory check, with potential DB fallback via `get_cell_data_hash`), a `LazyData::from_cell_meta` allocation, a redundant `binaries_by_data_hash` insert overwriting the same key, and a `Binaries::merge` call on `binaries_by_type_hash` if the cell has a type script. None of this work is cycle-metered.

The `DuplicateDepsVerifier` (`verification/src/transaction_verifier.rs`, lines 437–458) only checks for duplicate `CellDep` entries at the transaction level (i.e., duplicate dep group out-points in `cell_deps`); it does not inspect the contents of a dep group cell's `OutPointVec`. The integration test `test_duplicate_in_depgroup_type` (`test/src/specs/hardfork/v2021/cell_deps.rs`, lines 799–810) explicitly expects `PASS` for all combinations of duplicate sub-out-points within a dep group, confirming no rejection path exists.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single dep group cell (published once, costing a fixed amount of CKB for storage) can be referenced by an unlimited stream of transactions. Each such transaction causes every validating node to execute the `TxData::new` loop 2048 times instead of once—entirely unmetered overhead paid by the network, not the attacker. This overhead is incurred both during tx-pool admission and during block validation, which must complete within the block interval. A sustained stream of such transactions inflates per-transaction validation cost by up to ~2048×, degrading block propagation and potentially causing nodes to fall behind the chain tip.

## Likelihood Explanation
The attack requires no special privilege. Any RPC caller or P2P transaction relayer can:
1. Publish a live cell whose `outputs_data` is an `OutPointVec` containing 2048 copies of any valid live out-point. This cell is structurally valid and passes all non-contextual verifiers.
2. Submit transactions referencing that cell as a `DepGroup`-typed `CellDep`. These transactions pass `DuplicateDepsVerifier`, size checks, and all other validation layers.

The dep group cell is created once and reused across arbitrarily many transactions. The per-transaction cost to the attacker is only the normal transaction fee. The attack is cheap, repeatable, and requires no victim interaction.

## Recommendation
In `resolve_transaction_dep` (`util/types/src/core/cell.rs`), deduplicate `sub_out_points` before resolving and before decrementing `remaining_dep_slots`. A `HashSet` check is sufficient and should reject transactions containing internal duplicates:

```rust
let mut seen = HashSet::with_capacity(sub_out_points.len());
for sub_out_point in sub_out_points.into_iter() {
    if !seen.insert(sub_out_point.clone()) {
        return Err(OutPointError::InvalidDepGroup(outpoint));
    }
    *remaining_dep_slots = remaining_dep_slots
        .checked_sub(1)
        .ok_or(OutPointError::OverMaxDepExpansionLimit)?;
    resolved_cell_deps.push(cell_resolver(&sub_out_point, eager_load)?);
}
```

Alternatively, enforce strict ordering (by tx-hash then index) of sub-out-points within a dep group, which both prevents duplicates and enables binary search during lookup. The integration test `test_duplicate_in_depgroup_type` would need to be updated to expect rejection.

## Proof of Concept
1. Create a cell whose `outputs_data` is `OutPointVec::new_builder().set(vec![op; 2048]).build().as_bytes()` where `op` is any live out-point.
2. Submit a transaction with `cell_deps: [CellDep { out_point: <above cell>, dep_type: DepGroup }]`.
3. Observe the transaction is accepted into the tx-pool (passes `DuplicateDepsVerifier` and all other checks).
4. Observe that every node validating this transaction executes the loop in `TxData::new` 2048 times instead of once, with no additional cycle cost to the attacker.
5. Repeat step 2 with many transactions referencing the same dep group cell to sustain the amplified validation load across the network.