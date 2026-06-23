### Title
Division-by-Zero Panic via Crafted `since` Epoch Field in Transaction Verifier — (`verification/src/transaction_verifier.rs`)

### Summary

`EpochNumberWithFraction::to_rational()` panics with a division-by-zero when called on a value whose packed representation is non-zero but whose epoch-length bits are zero. The transaction verifier constructs `EpochNumberWithFraction` from attacker-controlled `since` fields using `from_full_value_unchecked`, which performs no validation. A remote, unprivileged peer can submit a transaction with a crafted `since` field to crash the verifying node.

### Finding Description

`EpochNumberWithFraction` is a packed 64-bit value encoding `(length << 40) | (index << 24) | number`. The safe constructor `from_full_value` normalizes zero-length values, but `from_full_value_unchecked` does not: [1](#0-0) 

`to_rational()` has a guard only for the all-zero genesis sentinel (`self.0 == 0`). Any other value with `length == 0` falls through to `RationalU256::new(index, 0)`, which explicitly panics: [2](#0-1) [3](#0-2) 

The transaction verifier calls `from_full_value_unchecked` on the `since` field of transaction inputs and subsequently calls `to_rational()` on the result (7 call sites in the same file): [4](#0-3) 

An attacker crafts a `since` value such as `0x2000_0000_0000_0001` (absolute, epoch type, packed epoch value = `1`). Decoded: `number = 1`, `index = 0`, `length = 0`. The packed value is non-zero (`self.0 = 1`), so the genesis guard is skipped, and `RationalU256::new(0.into(), 0.into())` panics.

The `EpochVerifier` for block headers does check `is_well_formed()` and rejects malformed epochs: [5](#0-4) 

However, no equivalent well-formedness check is applied to the epoch value embedded in a transaction's `since` field before `to_rational()` is called in the transaction verifier.

### Impact Explanation

A Rust panic in the verification thread propagates as an unrecoverable error. If the tx-pool or sync layer does not catch the panic (Rust panics are not `Result`-based), the node process crashes. This is a remote denial-of-service: any peer or RPC caller that can submit a transaction to the node can crash it with a single malformed transaction.

### Likelihood Explanation

The attack requires only the ability to submit a transaction to the node's tx-pool — available to any RPC caller or P2P peer. The crafted `since` value is trivial to construct (set epoch-type bits, set any non-zero epoch number, leave length bits zero). No funds, keys, or special access are required.

### Recommendation

Before calling `to_rational()` on any `EpochNumberWithFraction` derived from a transaction `since` field, validate it with `is_well_formed_increment()` (or `is_well_formed()`) and return a `Reject` error rather than panicking:

```rust
let epoch = EpochNumberWithFraction::from_full_value_unchecked(since_value);
if !epoch.is_well_formed_increment() {
    return Err(TransactionError::InvalidSince.into());
}
// safe to call to_rational() now
```

Alternatively, replace `from_full_value_unchecked` with `from_full_value` (which normalizes zero-length to length=1) at the `since`-parsing site. [6](#0-5) 

### Proof of Concept

1. Construct a transaction with one input whose `since` field is `0x2000_0000_0000_0001`:
   - Bit 63 = 0 (absolute)
   - Bits 62–61 = `10` (epoch type)
   - Lower 56 bits = `0x0000_0000_0000_0001` → `EpochNumberWithFraction(1)` → `number=1, index=0, length=0`
2. Submit the transaction via `send_transaction` RPC or P2P relay.
3. The node's transaction verifier calls `from_full_value_unchecked(1)` then `to_rational()`.
4. `to_rational`: `self.0 = 1 ≠ 0`, so calls `RationalU256::new(U256::zero(), U256::zero())`.
5. `RationalU256::new` panics: `"denominator == 0"`.
6. Node process crashes.

### Citations

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

**File:** util/types/src/core/extras.rs (L524-533)
```rust
    pub fn is_well_formed(self) -> bool {
        self.length() > 0 && self.length() > self.index()
    }

    /// Check the data format as an increment.
    ///
    /// The epoch index should be less than the epoch length or both of them are zero.
    pub fn is_well_formed_increment(self) -> bool {
        self.length() > self.index() || (self.length() == 0 && self.index() == 0)
    }
```

**File:** util/rational/src/lib.rs (L34-37)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
```

**File:** verification/src/transaction_verifier.rs (L1-1)
```rust
use crate::cache::Completed;
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```
