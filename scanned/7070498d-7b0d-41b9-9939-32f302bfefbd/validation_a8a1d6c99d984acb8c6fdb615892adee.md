All key claims are verified against the actual code. Let me summarize the findings:

**Claim 1 — Inflated fee rates during `assume_valid_target` IBD:**
- `chain/src/verify.rs` L233: `Switch::DISABLE_SCRIPT` is selected during IBD ✓
- `verification/contextual/src/contextual_block_verifier.rs` L690: `self.switch.disable_script()` is passed as `skip_script_verify` ✓
- `verification/src/transaction_verifier.rs` L165-166: `cycles = 0` when `skip_script_verify` is true ✓ (comment even says "skip script verify will result in the return value cycle always is zero")
- `util/types/src/core/tx_pool.rs` L298-303: `get_transaction_weight(size, 0) = max(size, 0) = size` — smaller weight → higher fee rate ✓
- `rpc/src/util/fee_rate.rs` L97-107: no guard for zero cycles or IBD state ✓

**Claim 2 — Panic when `txs_sizes` is `None`:**
- `chain/src/verify.rs` L718-723: `DISABLE_ALL` path calls `insert_ok_ext(..., None, None)` → `ext.txs_sizes = None` ✓
- `rpc/src/util/fee_rate.rs` L93: unconditional `.expect("expect txs_size's length >= 1")` ✓

**Impact assessment:**
- Inflated fee rates: real bug, but doesn't fit any allowed impact category (not a crash, not consensus deviation, not "easily" damaging CKB economy — it's temporary overpayment during IBD)
- Panic: fits "Any local RPC API crash" (Note, 0–500 points), but requires `ckb import --skip-all-verify` (admin operation)

---

Audit Report

## Title
`FeeRateCollector::statistics()` Panics on `None` `txs_sizes` from `DISABLE_ALL` Blocks — (File: `rpc/src/util/fee_rate.rs`)

## Summary
`FeeRateCollector::statistics()` unconditionally calls `.expect()` on `BlockExt.txs_sizes` at line 93 of `rpc/src/util/fee_rate.rs`. When blocks are processed with `Switch::DISABLE_ALL` (e.g., via `ckb import --skip-all-verify`), `insert_ok_ext` is called with `txs_sizes = None`, storing `None` in the database. Any subsequent call to `get_fee_rate_statistics` or `get_fee_rate_statics` over that block range panics, crashing the RPC task.

## Finding Description
In `chain/src/verify.rs`, the `reconcile_main_chain` function branches on `switch.disable_all()`. When true, it calls `insert_ok_ext` with `None` for both `cache_entries` and `txs_sizes`:

```rust
// chain/src/verify.rs L718-723
} else {
    txn.attach_block(b)?;
    attach_block_cell(&txn, b)?;
    mmr.push(b.digest())...;
    self.insert_ok_ext(&txn, &b.header().hash(), ext.clone(), None, None)?;
}
```

Inside `insert_ok_ext` (L758-777), `ext.txs_sizes = txs_sizes` stores `None` into the `BlockExt` written to the database. When `FeeRateCollector::statistics()` later iterates over these blocks via `FeeRateProvider::collect`, it destructures `BlockExt` and hits:

```rust
// rpc/src/util/fee_rate.rs L93
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

This unconditional `expect` panics for any `BlockExt` where `txs_sizes` is `None`. There is no `if let Some(...)` guard or early-continue for this case.

Additionally, during `assume_valid_target` IBD (the default for mainnet/testnet), `Switch::DISABLE_SCRIPT` is selected at `chain/src/verify.rs` L233. `ContextualBlockVerifier::verify` passes `self.switch.disable_script()` as `skip_script_verify` to `BlockTxsVerifier::verify` (L690). Inside `ContextualTransactionVerifier::verify` (`verification/src/transaction_verifier.rs` L165-166), `cycles = 0` when `skip_script_verify` is true. These zero-cycle `Completed` entries are stored via `insert_ok_ext`. `get_transaction_weight(size, 0)` returns `max(size, 0) = size`, a smaller weight than the true value, producing inflated fee rates from `FeeRate::calculate(fee, weight)`.

## Impact Explanation
The panic on `None` `txs_sizes` is a **local RPC API crash** (Note, 0–500 points). Any call to `get_fee_rate_statistics` after blocks have been imported with `--skip-all-verify` will panic and crash the RPC handler task. The inflated fee rate issue is a real data-quality bug but does not rise to a higher allowed impact class: it is temporary (limited to IBD + up to 101 blocks), affects only fee estimation accuracy, and does not crash the node or cause consensus deviation.

## Likelihood Explanation
The panic requires `ckb import --skip-all-verify`, an explicit admin/operator action. After such an import, the condition is persistent in the database and triggered by any unprivileged RPC caller invoking `get_fee_rate_statistics`. The inflated fee rates affect all default mainnet/testnet nodes during IBD, but the impact is limited to fee overpayment.

## Recommendation
1. Replace the unconditional `txs_sizes.expect(...)` at `rpc/src/util/fee_rate.rs` L93 with `if let Some(txs_sizes) = txs_sizes` and skip the block if `None`, preventing the panic.
2. Optionally filter out `BlockExt` entries where `cycles` is all-zeros (or where the node was in `assume_valid_target` mode) to avoid returning inflated fee rates during IBD, analogous to the `is_ready` guard in `FeeEstimator`.

## Proof of Concept
1. Run `ckb import --skip-all-verify <export_file>` to populate the database with `BlockExt` entries where `txs_sizes = None`.
2. Start the node normally.
3. Call `get_fee_rate_statistics` via RPC.
4. Observe the RPC task panics with `"expect txs_size's length >= 1"` at `rpc/src/util/fee_rate.rs` L93.