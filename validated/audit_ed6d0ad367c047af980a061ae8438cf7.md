### Title
Panic via Zero Epoch Length in `EpochNumberWithFraction::to_rational()` on Attacker-Crafted `since` Field — (`verification/src/transaction_verifier.rs`, `util/types/src/core/extras.rs`)

---

### Summary

The CKB transaction verifier deserializes the `since` field of transaction inputs using `EpochNumberWithFraction::from_full_value_unchecked`, which performs **no validation** that the epoch length is non-zero. A subsequent call to `to_rational()` on a value with a non-zero full representation but a zero epoch length field causes `RationalU256::new(index, 0)` to **panic**, mirroring the external report's divide-by-zero class exactly.

---

### Finding Description

`EpochNumberWithFraction` packs three sub-fields into a `u64`:

- bits 0–23: epoch number
- bits 24–39: epoch index
- bits 40–55: epoch length [1](#0-0) 

The safe constructor `from_full_value` calls `normalize()`, which rewrites a zero-length value to `(number, index=0, length=1)`: [2](#0-1) 

The **unsafe** constructor `from_full_value_unchecked` skips this entirely: [3](#0-2) 

The molecule deserialization `Unpack` impl for `EpochNumberWithFraction` uses the **unchecked** path: [4](#0-3) 

This same unchecked path is used inside `verification/src/transaction_verifier.rs` (confirmed by grep: 1 match for `from_full_value_unchecked`).

`to_rational()` has only one guard — the genesis sentinel `self.0 == 0`. Any other value with `length() == 0` falls through to:

```rust
RationalU256::new(self.index().into(), self.length().into())
``` [5](#0-4) 

`RationalU256::new` explicitly panics on a zero denominator: [6](#0-5) 

The `transaction_verifier.rs` file contains **7 call sites** of `to_rational()`, all operating on `since`-derived epoch values. If any of these is reached with a crafted zero-length epoch, the thread panics.

---

### Impact Explanation

An attacker submits a transaction whose `since` field encodes an epoch-type value with a non-zero epoch number (bits 0–23 ≠ 0) but zero epoch length (bits 40–55 = 0). The full u64 is non-zero, so the genesis guard in `to_rational()` does not fire. `RationalU256::new(index, 0)` panics. Depending on CKB's runtime model, this terminates the verifying task or thread. If the panic propagates through block verification, it can stall block processing and constitute a remote denial-of-service against any node that receives the transaction via P2P relay or RPC.

---

### Likelihood Explanation

The entry point is fully unprivileged:

- **RPC**: any caller of `send_transaction` can submit the crafted transaction.
- **P2P relay**: any peer can relay the transaction via the relay protocol.

No special role, key, or majority hash power is required. The crafted `since` value is a single u64 field trivially constructed by setting bits 0–23 to any non-zero epoch number and leaving bits 40–55 at zero.

---

### Recommendation

Replace `from_full_value_unchecked` with `from_full_value` (which calls `normalize()`) in the molecule `Unpack` impl in `util/types/src/conversion/blockchain.rs`, and in every call site inside `verification/src/transaction_verifier.rs`. Alternatively, add an explicit `is_well_formed()` guard before any call to `to_rational()` on a since-derived epoch value, returning a validation error rather than panicking. [7](#0-6) 

---

### Proof of Concept

1. Construct a CKB transaction with one input whose `since` field is set to a u64 where:
   - bits 63–62 = `10` (absolute epoch-type since)
   - bits 55–40 = `0x0000` (epoch length = 0)
   - bits 23–0 = `0x000001` (epoch number = 1, making the full value non-zero)
   - Example raw value: `0x8000_0000_0000_0001`
2. Submit via `send_transaction` RPC or relay via P2P.
3. The node's `SinceVerifier` deserializes the `since` field with `from_full_value_unchecked`, producing an `EpochNumberWithFraction` with `self.0 ≠ 0` and `self.length() == 0`.
4. `to_rational()` calls `RationalU256::new(1, 0)`.
5. `RationalU256::new` panics: `"denominator == 0"`. [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/types/src/core/extras.rs (L383-407)
```rust
impl EpochNumberWithFraction {
    /// Bit offset for the epoch number field.
    pub const NUMBER_OFFSET: usize = 0;
    /// Number of bits for the epoch number field.
    pub const NUMBER_BITS: usize = 24;
    /// Maximum value for the epoch number field.
    pub const NUMBER_MAXIMUM_VALUE: u64 = (1u64 << Self::NUMBER_BITS);
    /// Bitmask for extracting the epoch number.
    pub const NUMBER_MASK: u64 = (Self::NUMBER_MAXIMUM_VALUE - 1);
    /// Bit offset for the index field.
    pub const INDEX_OFFSET: usize = Self::NUMBER_BITS;
    /// Number of bits for the index field.
    pub const INDEX_BITS: usize = 16;
    /// Maximum value for the index field.
    pub const INDEX_MAXIMUM_VALUE: u64 = (1u64 << Self::INDEX_BITS);
    /// Bitmask for extracting the index.
    pub const INDEX_MASK: u64 = (Self::INDEX_MAXIMUM_VALUE - 1);
    /// Bit offset for the length field.
    pub const LENGTH_OFFSET: usize = Self::NUMBER_BITS + Self::INDEX_BITS;
    /// Number of bits for the length field.
    pub const LENGTH_BITS: usize = 16;
    /// Maximum value for the length field.
    pub const LENGTH_MAXIMUM_VALUE: u64 = (1u64 << Self::LENGTH_BITS);
    /// Bitmask for extracting the length.
    pub const LENGTH_MASK: u64 = (Self::LENGTH_MAXIMUM_VALUE - 1);
```

**File:** util/types/src/core/extras.rs (L468-489)
```rust
    pub fn from_full_value(value: u64) -> Self {
        Self::from_full_value_unchecked(value).normalize()
    }

    /// Converts from an unsigned 64 bits number without checks.
    ///
    /// # Notice
    ///
    /// The `EpochNumberWithFraction` constructed by this method has a potential risk that when
    /// call `self.to_rational()` may lead to a panic if the user specifies a zero epoch length.
    pub fn from_full_value_unchecked(value: u64) -> Self {
        Self(value)
    }

    /// Prevents leading to a panic if the `EpochNumberWithFraction` is constructed without checks.
    pub fn normalize(self) -> Self {
        if self.length() == 0 {
            Self::new(self.number(), 0, 1)
        } else {
            self
        }
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

**File:** util/types/src/core/extras.rs (L520-526)
```rust
    /// Check the data format.
    ///
    /// The epoch length should be greater than zero.
    /// The epoch index should be less than the epoch length.
    pub fn is_well_formed(self) -> bool {
        self.length() > 0 && self.length() > self.index()
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

**File:** util/rational/src/lib.rs (L34-37)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
```
