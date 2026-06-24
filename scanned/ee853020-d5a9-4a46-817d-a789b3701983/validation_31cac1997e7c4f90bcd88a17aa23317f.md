Audit Report

## Title
`EpochNumberWithFraction::to_rational()` Panics on Zero-Length Epoch via Crafted `since` Field — (`util/types/src/core/extras.rs`)

## Summary

`EpochNumberWithFraction::to_rational()` guards against division-by-zero by checking whether the full packed 64-bit value is zero, but the actual division uses only the `length` sub-field. An attacker can craft a transaction `since` field with `number=1, index=0, length=0` (raw value `0x0000000000000001`), which bypasses the guard (`self.0 != 0`) but causes `RationalU256::new()` to panic with `"denominator == 0"` when `self.length() == 0`. The `since` field is parsed via `from_full_value_unchecked` in `SinceVerifier`, which skips the `normalize()` call that would otherwise prevent this, crashing the node's verification thread.

## Finding Description

`to_rational()` in `util/types/src/core/extras.rs` (L496–502) guards with `if self.0 == 0` (the full 64-bit packed value), but divides by `self.length()` (only bits 40–55):

```rust
pub fn to_rational(self) -> RationalU256 {
    if self.0 == 0 {                    // checks FULL packed value
        RationalU256::zero()
    } else {
        RationalU256::new(self.index().into(), self.length().into())  // divides by LENGTH sub-field
            + U256::from(self.number())
    }
}
```

`RationalU256::new()` in `util/rational/src/lib.rs` (L34–37) unconditionally panics when the denominator is zero:

```rust
pub fn new(numer: U256, denom: U256) -> RationalU256 {
    if denom.is_zero() {
        panic!("denominator == 0");
    }
```

The safe constructor `from_full_value()` (L468–469) calls `.normalize()`, which rewrites zero-length epochs to `length=1`. However, `from_full_value_unchecked()` (L478–479) skips this. The docstring on `from_full_value_unchecked` explicitly acknowledges the risk:

> "The `EpochNumberWithFraction` constructed by this method has a potential risk that when call `self.to_rational()` may lead to a panic if the user specifies a zero epoch length."

Despite this warning, `from_full_value_unchecked` is used once in `verification/src/transaction_verifier.rs` in the `SinceVerifier` path, and `to_rational()` is called 7 times in that same file. The `since` field of a `CellInput` is a raw 64-bit value fully controlled by the transaction submitter. When the metric type flag encodes an epoch value, the node parses it via `from_full_value_unchecked` and calls `to_rational()` during `SinceVerifier::verify()`, which is invoked during both tx-pool admission and block verification.

Exploit path:
1. Attacker crafts `since` with `number=1, index=0, length=0` → raw epoch value `0x0000000000000001`, combined with epoch metric type flag: `0x2000000000000001`.
2. `from_full_value_unchecked(0x0000000000000001)` → `EpochNumberWithFraction(1)`.
3. `to_rational()`: `self.0 = 1 ≠ 0`, guard passes.
4. `self.length() = 0` → `RationalU256::new(0, 0)` → `panic!("denominator == 0")`.

## Impact Explanation

A Rust `panic!` unwinds the calling thread. If the verification thread is not wrapped in `catch_unwind`, the panic propagates and terminates the thread, causing the node process to crash or the verification service to become unresponsive. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.** The attack is repeatable after any restart.

## Likelihood Explanation

No privilege is required. Any RPC caller can invoke `send_raw_transaction` with a crafted `since` field, or any P2P peer can relay such a transaction. The crafted value is trivial to construct. No mining, staking, or key material is needed.

## Recommendation

Replace the guard in `to_rational()` to check `self.length()` specifically, matching the logic already present in `normalize()`:

```rust
pub fn to_rational(self) -> RationalU256 {
    if self.length() == 0 {
        RationalU256::zero()
    } else {
        RationalU256::new(self.index().into(), self.length().into())
            + U256::from(self.number())
    }
}
```

Alternatively, replace `from_full_value_unchecked` with `from_full_value` at every site in `verification/src/transaction_verifier.rs` where the `since` field is parsed from untrusted input.

## Proof of Concept

1. Craft a transaction with one input whose `since` field = `0x2000000000000001` (epoch metric type flag `0x2000000000000000` OR'd with epoch value `0x0000000000000001`, encoding `number=1, index=0, length=0`).
2. Submit via `send_raw_transaction` RPC to any CKB node.
3. The node calls `SinceVerifier::verify()` → `from_full_value_unchecked(0x0000000000000001)` → `to_rational()` on `EpochNumberWithFraction(1)` → `RationalU256::new(0, 0)` → `panic!("denominator == 0")`.
4. The verification thread panics; the node crashes or its verification service becomes unresponsive.