Audit Report

## Title
`FeeRateCollector::statistics()` Unconditional `.expect()` on `txs_sizes` Panics for Pre-`BlockExtV1` Blocks — (`File: rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics()` calls `.expect()` on `block_ext.txs_sizes` without any guard on whether the field is populated. `txs_sizes` is `None` for any `BlockExt` stored in the old (pre-`BlockExtV1`) 5-field format, which `get_block_ext` returns verbatim. Any unprivileged caller of `get_fee_rate_statistics` on a node with unmigrated old-format blocks triggers a panic in the RPC handler.

## Finding Description

`get_block_ext` in `store/src/store.rs` (L247–263) distinguishes old from new format by `count_extra_fields()`: when the stored record has 0 extra fields (old `BlockExt`, 5 fields), it deserializes via `packed::BlockExtReader`, whose `From` impl in `util/types/src/conversion/storage.rs` (L154–165) hard-codes `cycles: None, txs_sizes: None`. When the record has 2 extra fields (`BlockExtV1`, 7 fields), it deserializes via `packed::BlockExtV1Reader`, which populates both fields.

`FeeRateProvider::collect` in `rpc/src/util/fee_rate.rs` (L35–48) iterates canonical-chain block numbers and calls `get_block_ext_by_number`, filtering only on `Option::Some` — no check on `verified` or `txs_sizes`. The fold closure in `statistics()` (L86–111) then unconditionally calls:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

at L93. If any block in the fee-rate window was stored before `BlockExtV1` was introduced, `txs_sizes` is `None` and this line panics.

A second path exists: `reconcile_main_chain` in `chain/src/verify.rs` (L723) calls `insert_ok_ext` with `txs_sizes: None` when `switch.disable_all()` is active, producing a stored `BlockExtV1` with `verified = Some(true)` but `txs_sizes = None`. These blocks are also attached to the canonical chain index and returned by `get_block_ext_by_number`.

The unit test in `rpc/src/tests/fee_rate.rs` (L47–76) constructs `BlockExt` with `verified: None` but always supplies `txs_sizes: Some(...)`, so it never exercises the `None` branch and provides no regression coverage for this path.

## Impact Explanation

The panic propagates out of `statistics()` through `Iterator::fold`. Depending on whether the JSON-RPC server wraps handlers in `catch_unwind`, this either crashes the serving thread/task or returns an internal error, making the `get_fee_rate_statistics` (and `estimate_fee_rate`) RPC endpoint unavailable. This matches the allowed CKB bounty impact: **Any local RPC API crash (Note, 0–500 points)**.

## Likelihood Explanation

The condition is met on any CKB node that was operational before `BlockExtV1` was introduced and whose database has not been fully migrated. A single unprivileged call to `get_fee_rate_statistics` with a `target` window large enough to include an old-format block is sufficient to trigger the panic. No keys, privileges, or majority hash power are required.

## Recommendation

In the `collect` closure inside `statistics()`, replace the unconditional `.expect()` with a graceful skip:

```rust
let txs_sizes = match txs_sizes {
    Some(s) if s.len() >= 1 => s,
    _ => return fee_rates,
};
```

Optionally, also skip blocks where `block_ext.verified != Some(true)` in `FeeRateProvider::collect` to prevent unverified or failure-ext blocks from entering the window.

## Proof of Concept

1. Run a CKB node that was active before `BlockExtV1` was introduced, so that some canonical-chain blocks have `BlockExt` stored in the old 5-field format (deserialized with `txs_sizes: None`).
2. Call `get_fee_rate_statistics` via RPC with a `target` window that includes one of those old-format blocks.
3. `FeeRateProvider::collect` fetches those `BlockExt` records via `get_block_ext_by_number`; `get_block_ext` returns them with `txs_sizes: None` (via the `count_extra_fields() == 0` branch in `store/src/store.rs` L252–253).
4. `statistics()` reaches `txs_sizes.expect("expect txs_size's length >= 1")` with `txs_sizes = None` and panics.

Alternatively, reproduce in a unit test by inserting a `BlockExt` with `txs_sizes: None` into `DummyFeeRateProvider` and calling `FeeRateCollector::new(&provider).statistics(None)` — the existing test harness in `rpc/src/tests/fee_rate.rs` already provides the scaffolding; simply omit `txs_sizes` (set to `None`) in one entry.