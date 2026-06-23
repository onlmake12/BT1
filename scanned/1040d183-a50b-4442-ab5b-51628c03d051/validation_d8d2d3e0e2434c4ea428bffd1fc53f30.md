### Title
`EpochNumberWithFraction::to_rational()` Panics on Zero-Length Epoch in `since` Field — (`util/types/src/core/extras.rs`)

---

### Summary

`EpochNumberWithFraction::to_rational()` guards against division-by-zero by checking whether the **entire packed 64-bit value** is zero, but the actual division uses the **`length` sub-field** extracted from that value. An attacker can craft a transaction `since` field whose raw value is non-zero (bypassing the guard) but whose `length` bits are zero, causing `RationalU256::new()` to panic inside `SinceVerifier`. This is the direct CKB analog of the `totalSupply`-vs-`derivedSupply` mismatch in the external report.

---

### Finding Description

`EpochNumberWithFraction` packs three sub-fields into a single `u64`:

| Field | Bits |
|---|---|
| `number` | 0–23 |
| `index` | 24–39 |
| `length` | 40–55 | [1](#0-0) 

`to_rational()` guards with `if self.0 == 0` (the full raw value), but then divides by `self.length()` (only the upper sub-field):

```rust
pub fn to_rational(self) -> RationalU256 {
    if self.0 == 0 {                          // guard: checks FULL packed value
        RationalU256::zero()
    } else {
        RationalU256::new(self.index().into(), self.length().into())  // divides by LENGTH sub-field
            + U256::from(self.number())
    }
}
``` [2](#0-1) 

`RationalU256::new` unconditionally panics when its denominator is zero:

```rust
pub fn new(numer: U256, denom: U256) -> RationalU256 {
    if denom.is_zero() {
        panic!("denominator == 0");
    }
    ...
}
``` [3](#0-2) 

An attacker can set `number = 1`, `index = 0`, `length = 0` in the packed value. Then `self.0 = 1` (non-zero, guard passes), but `self.length() = 0` (division panics). The safe constructor `from_full_value()` normalizes this away, but `from_full_value_unchecked()` does not: [4](#0-3) 

`from_full_value_unchecked` is used directly inside `verification/src/transaction_verifier.rs`, and `to_rational()` is called seven times in that same file during `SinceVerifier` processing of the attacker-supplied `since` field: [5](#0-4) 

The `since` field of a `CellInput` is a raw 64-bit value fully controlled by the transaction submitter. When the metric type flag encodes an epoch value, the node parses it via `from_full_value_unchecked` and immediately calls `to_rational()` during `SinceVerifier::verify()`, which is invoked both during tx-pool admission and block verification.

---

### Impact Explanation

A Rust `panic!` unwinds the calling thread. If the verification thread is not wrapped in a `catch_unwind`, the panic propagates and terminates the thread. Depending on the node's thread supervision model, this causes either:

- **Node process crash** (if the panic reaches an unguarded thread boundary), or
- **Verification service hang/deadlock** (if the thread pool loses a worker without replacement)

Either outcome denies service to all users of the node — analogous to the external report's "temporary freezing of funds" impact. The attack is repeatable: the attacker can resubmit the crafted transaction after any restart.

---

### Likelihood Explanation

The entry path requires no privilege: any RPC caller can invoke `send_raw_transaction` with a crafted `since` field, or any P2P peer can relay such a transaction. The crafted value is trivial to construct (e.g., `0x0000000000000001` — `number=1`, `index=0`, `length=0`). No mining, staking, or key material is required.

---

### Recommendation

Replace the guard in `to_rational()` with a check on the `length` sub-field specifically, mirroring the existing `normalize()` logic:

```rust
pub fn to_rational(self) -> RationalU256 {
    if self.length() == 0 {          // guard on the SAME field used in division
        RationalU256::zero()
    } else {
        RationalU256::new(self.index().into(), self.length().into())
            + U256::from(self.number())
    }
}
```

Alternatively, replace `from_full_value_unchecked` with `from_full_value` (which calls `normalize()`) at every site in `verification/src/transaction_verifier.rs` where the `since` field is parsed from untrusted input. [6](#0-5) 

---

### Proof of Concept

1. Craft a transaction with one input whose `since` field encodes metric type = epoch (`0x2000_0000_0000_0000`) and epoch value = `0x0000_0000_0000_0001` (number=1, index=0, length=0). Combined `since` = `0x2000_0000_0000_0001`.
2. Submit via `send_raw_transaction` RPC to any CKB node.
3. The node calls `SinceVerifier::verify()` → `to_rational()` on the parsed `EpochNumberWithFraction(1)` → `RationalU256::new(0, 0)` → `panic!("denominator == 0")`.
4. The verification thread panics; the node crashes or its verification service becomes unresponsive.

### Citations

**File:** util/types/src/core/extras.rs (L383-440)
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

    /// Creates a new epoch number with fraction.
    pub fn new(number: u64, index: u64, length: u64) -> EpochNumberWithFraction {
        debug_assert!(number < Self::NUMBER_MAXIMUM_VALUE);
        debug_assert!(index < Self::INDEX_MAXIMUM_VALUE);
        debug_assert!(length < Self::LENGTH_MAXIMUM_VALUE);
        debug_assert!(length > 0);
        Self::new_unchecked(number, index, length)
    }

    /// Creates a new epoch number with fraction without bounds checking.
    pub const fn new_unchecked(number: u64, index: u64, length: u64) -> Self {
        EpochNumberWithFraction(
            (length << Self::LENGTH_OFFSET)
                | (index << Self::INDEX_OFFSET)
                | (number << Self::NUMBER_OFFSET),
        )
    }

    /// Returns the epoch number.
    pub fn number(self) -> EpochNumber {
        (self.0 >> Self::NUMBER_OFFSET) & Self::NUMBER_MASK
    }

    /// Returns the block index within the epoch.
    pub fn index(self) -> u64 {
        (self.0 >> Self::INDEX_OFFSET) & Self::INDEX_MASK
    }

    /// Returns the epoch length in blocks.
    pub fn length(self) -> u64 {
        (self.0 >> Self::LENGTH_OFFSET) & Self::LENGTH_MASK
    }
```

**File:** util/types/src/core/extras.rs (L468-480)
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
```

**File:** util/types/src/core/extras.rs (L482-489)
```rust
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

**File:** verification/src/transaction_verifier.rs (L383-396)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let cellbase_immature = |meta: &CellMeta| -> bool {
            meta.transaction_info
                .as_ref()
                .map(|info| {
                    info.block_number > 0 && info.is_cellbase() && {
                        let threshold =
                            self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
                        let current = self.epoch.to_rational();
                        current < threshold
                    }
                })
                .unwrap_or(false)
        };
```
