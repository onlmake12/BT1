### Title
Insufficient Epoch Parameter Validation Causes Panic in `to_rational()` During Since Verification — (`util/types/src/core/extras.rs`, `util/types/src/conversion/blockchain.rs`, `verification/src/transaction_verifier.rs`)

---

### Summary

The deserialization of `EpochNumberWithFraction` from network messages uses `from_full_value_unchecked`, which skips the zero-length epoch guard. When `to_rational()` is subsequently called on such a value during transaction `since` verification, it invokes `RationalU256::new(index, 0)`, which unconditionally panics. An unprivileged transaction sender can craft a `since` field encoding a zero-length epoch to trigger this panic in the verifier.

---

### Finding Description

`EpochNumberWithFraction` has two construction paths from a raw `u64`:

- `from_full_value(value)` — calls `.normalize()`, rewriting zero-length epochs to length=1 to prevent panics.
- `from_full_value_unchecked(value)` — stores the raw bits with no guard.

The safe path is documented explicitly: [1](#0-0) 

The unsafe path is documented as dangerous: [2](#0-1) 

Despite this, the `Unpack` impl used for all network-received block header epoch fields uses the **unchecked** variant: [3](#0-2) 

And the `From` impl for `packed::Uint64Reader` also uses the unchecked path: [4](#0-3) 

Additionally, `from_full_value_unchecked` appears in `verification/src/transaction_verifier.rs` — meaning the since verifier itself parses the epoch embedded in a transaction's `since` u64 field without normalization.

When `to_rational()` is called on an `EpochNumberWithFraction` where `self.0 != 0` but `self.length() == 0`, the code reaches: [5](#0-4) 

This calls `RationalU256::new(index.into(), 0.into())`, which unconditionally panics: [6](#0-5) 

The panic is documented but the guard is absent on the deserialization and since-parsing paths.

---

### Impact Explanation

A Rust `panic!` in a non-`catch_unwind` context terminates the thread. If the since verifier or block header processor runs in a thread without panic isolation, the node process crashes. Even with unwinding, a panic propagating through the tx-pool admission or block verification pipeline causes the node to reject all subsequent work until restarted, constituting a Denial of Service. The attacker does not need any funds locked in the crafted transaction to be valid on-chain — the panic fires during the validation step before any state change.

---

### Likelihood Explanation

Submitting a transaction to the tx-pool via RPC is an unprivileged, zero-cost operation. The `since` field is a raw `u64` in the transaction input, fully attacker-controlled. Crafting a value with epoch number ≥ 1 and epoch length = 0 is trivial (e.g., `(0 << LENGTH_OFFSET) | (0 << INDEX_OFFSET) | (1 << NUMBER_OFFSET)` = `0x0000000000000001`). No special access, hashpower, or key material is required.

---

### Recommendation

**Short term:** Replace `from_full_value_unchecked` with `from_full_value` (which calls `.normalize()`) in both the `Unpack` impl in `util/types/src/conversion/blockchain.rs` and in the since-field parsing in `verification/src/transaction_verifier.rs`. Alternatively, add an explicit length-zero check before calling `to_rational()` and return a validation error rather than panicking.

**Long term:** Audit all call sites of `to_rational()` to ensure every `EpochNumberWithFraction` reaching that call has been constructed through the safe (`from_full_value` / `new`) path. Consider using the Rust fuzzer (`cargo-fuzz`) targeting the since verifier and block header deserializer to confirm no other panic paths exist.

---

### Proof of Concept

1. Construct a transaction input whose `since` field is `0x0000000000000001` (epoch number = 1, index = 0, length = 0, with the epoch-type flag set).
2. Submit the transaction via the `send_transaction` RPC to any CKB node.
3. The node's since verifier calls `from_full_value_unchecked(0x0000000000000001)`, producing an `EpochNumberWithFraction` with `number()=1`, `index()=0`, `length()=0`.
4. `to_rational()` is called: `self.0 != 0`, so it proceeds to `RationalU256::new(0.into(), 0.into())`.
5. `RationalU256::new` panics: `"denominator == 0"`.
6. The verifier thread panics, causing a node crash or unhandled error propagation. [5](#0-4) [7](#0-6) [3](#0-2)

### Citations

**File:** util/types/src/core/extras.rs (L462-470)
```rust
    /// Creates an epoch number with fraction from a packed 64-bit value.
    // One caveat here, is that if the user specifies a zero epoch length either
    // deliberately, or by accident, calling to_rational() after that might
    // result in a division by zero panic. To prevent that, this method would
    // automatically rewrite the value to epoch index 0 with epoch length to
    // prevent panics
    pub fn from_full_value(value: u64) -> Self {
        Self::from_full_value_unchecked(value).normalize()
    }
```

**File:** util/types/src/core/extras.rs (L472-480)
```rust
    /// Converts from an unsigned 64 bits number without checks.
    ///
    /// # Notice
    ///
    /// The `EpochNumberWithFraction` constructed by this method has a potential risk that when
    /// call `self.to_rational()` may lead to a panic if the user specifies a zero epoch length.
    pub fn from_full_value_unchecked(value: u64) -> Self {
        Self(value)
    }
```

**File:** util/types/src/core/extras.rs (L496-502)
```rust
    pub fn to_rational(self) -> RationalU256 {
        if self.0 == 0 {
            RationalU256::zero()
        } else {
            RationalU256::new(self.index().into(), self.length().into()) + U256::from(self.number())
        }
    }
```

**File:** util/types/src/conversion/blockchain.rs (L21-25)
```rust
impl<'r> Unpack<core::EpochNumberWithFraction> for packed::Uint64Reader<'r> {
    fn unpack(&self) -> core::EpochNumberWithFraction {
        core::EpochNumberWithFraction::from_full_value_unchecked(self.unpack())
    }
}
```

**File:** util/types/src/conversion/blockchain.rs (L28-32)
```rust
impl<'r> From<packed::Uint64Reader<'r>> for core::EpochNumberWithFraction {
    fn from(value: packed::Uint64Reader<'r>) -> core::EpochNumberWithFraction {
        core::EpochNumberWithFraction::from_full_value_unchecked(value.into())
    }
}
```

**File:** util/rational/src/lib.rs (L34-41)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
        let mut ret = RationalU256::new_raw(numer, denom);
        ret.reduce();
        ret
    }
```
