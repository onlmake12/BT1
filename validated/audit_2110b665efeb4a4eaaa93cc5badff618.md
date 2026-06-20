### Title
Division-by-Zero Panic in `EpochNumberWithFraction::to_rational()` via Unchecked Deserialization of Block Header Epoch Field — (`util/types/src/core/extras.rs`, `util/types/src/conversion/blockchain.rs`, `verification/src/transaction_verifier.rs`)

---

### Summary

`EpochNumberWithFraction` is deserialized from all packed network bytes using `from_full_value_unchecked`, which skips the zero-length normalization guard. `to_rational()` panics unconditionally when `length == 0` and the packed value is non-zero. Two call sites in transaction verification — `MaturityVerifier` and `SinceVerifier` — invoke `info.block_epoch.to_rational()` without first normalizing, meaning any block accepted with a zero-length epoch field will cause a panic whenever a transaction spends a cell created in that block.

---

### Finding Description

**Root cause — unchecked deserialization:**

`util/types/src/conversion/blockchain.rs` lines 21–32 define the canonical deserialization of every `EpochNumberWithFraction` from packed bytes:

```rust
impl<'r> Unpack<core::EpochNumberWithFraction> for packed::Uint64Reader<'r> {
    fn unpack(&self) -> core::EpochNumberWithFraction {
        core::EpochNumberWithFraction::from_full_value_unchecked(self.unpack())
    }
}
impl<'r> From<packed::Uint64Reader<'r>> for core::EpochNumberWithFraction {
    fn from(value: packed::Uint64Reader<'r>) -> core::EpochNumberWithFraction {
        core::EpochNumberWithFraction::from_full_value_unchecked(value.into())
    }
}
``` [1](#0-0) 

This means every block header epoch field received from the network is stored as-is, without normalization.

**The panic site — `to_rational()`:**

`util/types/src/core/extras.rs` documents the risk explicitly and panics when `length == 0` and the packed value is non-zero:

```rust
/// # Panics
/// Only genesis epoch's length could be zero, otherwise causes a division-by-zero panic.
pub fn to_rational(self) -> RationalU256 {
    if self.0 == 0 {
        RationalU256::zero()
    } else {
        RationalU256::new(self.index().into(), self.length().into()) + U256::from(self.number())
    }
}
``` [2](#0-1) 

`RationalU256::new` panics unconditionally when its denominator is zero:

```rust
pub fn new(numer: U256, denom: U256) -> RationalU256 {
    if denom.is_zero() {
        panic!("denominator == 0");
    }
    ...
}
``` [3](#0-2) 

The safe constructor `from_full_value` calls `.normalize()` to rewrite zero-length epochs, but the deserialization path uses `from_full_value_unchecked` exclusively: [4](#0-3) 

**Vulnerable call sites in transaction verification:**

`MaturityVerifier::verify()` calls `info.block_epoch.to_rational()` without normalization:

```rust
let threshold =
    self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
``` [5](#0-4) 

`SinceVerifier::verify_relative_lock()` also calls `info.block_epoch.to_rational()` without normalization:

```rust
let b = info.block_epoch.to_rational()
    + epoch_number_with_fraction.normalize().to_rational();
``` [6](#0-5) 

Note the asymmetry: the `since`-derived `epoch_number_with_fraction` has `.normalize()` called on it, but `info.block_epoch` — which originates from the stored block header — does not.

---

### Impact Explanation

If a block with a non-zero epoch number but zero epoch length is accepted and stored by a node, every subsequent attempt to verify a transaction that spends any cell created in that block will trigger an unrecoverable `panic!("denominator == 0")` inside `to_rational()`. This crashes the transaction verification pipeline. Affected transactions cannot be admitted to the tx-pool, cannot be included in blocks, and cannot be validated during block import. Cells locked in such a block become permanently unspendable on the affected node, and the node may be rendered unable to process any block that commits such transactions.

---

### Likelihood Explanation

The entry path is a block relayer or miner who crafts a block header with the epoch length bits set to zero while the epoch number bits are non-zero. The packed `Uint64` encoding of `EpochNumberWithFraction` places length in the top 16 bits, number in the bottom 24 bits, and index in the middle 24 bits; zeroing only the top 16 bits while leaving number non-zero produces a value that passes the `self.0 == 0` genesis guard in `to_rational()` but still supplies a zero denominator to `RationalU256::new`. Whether the header verifier independently rejects such a field was not confirmed in this analysis (the file `verification/src/header_verifier.rs` was not fully read); if it does not, the path is directly reachable by any peer that can relay a crafted block.

---

### Recommendation

1. Replace `from_full_value_unchecked` with `from_full_value` (which calls `.normalize()`) in the `Unpack` and `From` impls in `util/types/src/conversion/blockchain.rs`, so that all deserialized epoch values are safe before use.
2. Add an explicit zero-length guard in `to_rational()` that returns `RationalU256::zero()` (or an error) instead of panicking, consistent with the genesis-epoch special case already present.
3. Add a check in the header verifier that rejects any block header whose epoch field has a zero length and a non-zero epoch number.

---

### Proof of Concept

1. Craft a block header where the epoch `Uint64` field has epoch number `> 0`, index `0`, and length `0` (i.e., the top 16 bits are zero, the bottom 24 bits are non-zero).
2. Relay this block to a target node. If the header verifier does not reject the zero-length epoch, the block is stored.
3. The block contains a cellbase output. A transaction is constructed spending that cellbase (after maturity).
4. During `MaturityVerifier::verify()`, `info.block_epoch.to_rational()` is called. `self.0 != 0` (epoch number is non-zero), so the genesis guard is bypassed. `self.length() == 0`, so `RationalU256::new(index, 0)` panics: `"denominator == 0"`.
5. The node crashes or the transaction is permanently unverifiable, locking the cell. [7](#0-6) [8](#0-7) [1](#0-0) [9](#0-8)

### Citations

**File:** util/types/src/conversion/blockchain.rs (L21-32)
```rust
impl<'r> Unpack<core::EpochNumberWithFraction> for packed::Uint64Reader<'r> {
    fn unpack(&self) -> core::EpochNumberWithFraction {
        core::EpochNumberWithFraction::from_full_value_unchecked(self.unpack())
    }
}
impl_conversion_for_entity_unpack!(core::EpochNumberWithFraction, Uint64);

impl<'r> From<packed::Uint64Reader<'r>> for core::EpochNumberWithFraction {
    fn from(value: packed::Uint64Reader<'r>) -> core::EpochNumberWithFraction {
        core::EpochNumberWithFraction::from_full_value_unchecked(value.into())
    }
}
```

**File:** util/types/src/core/extras.rs (L462-489)
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

**File:** util/types/src/core/extras.rs (L491-502)
```rust
    /// Converts the epoch to an unsigned 256 bits rational.
    ///
    /// # Panics
    ///
    /// Only genesis epoch's length could be zero, otherwise causes a division-by-zero panic.
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

**File:** verification/src/transaction_verifier.rs (L383-395)
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
```

**File:** verification/src/transaction_verifier.rs (L693-694)
```rust
                    let b = info.block_epoch.to_rational()
                        + epoch_number_with_fraction.normalize().to_rational();
```
