### Title
`EpochNumberWithFraction::from_full_value_unchecked` Used in Deserialization Allows Crafted Zero-Length Epoch to Panic Node via `to_rational()` — (`util/types/src/conversion/blockchain.rs`)

---

### Summary

The deserialization of `EpochNumberWithFraction` from packed network bytes uses `from_full_value_unchecked`, which skips the zero-length normalization guard. A crafted packed `Uint64` with `length == 0` but `number != 0` or `index != 0` will produce an `EpochNumberWithFraction` that panics when `to_rational()` is called. The `SinceVerifier` in transaction verification calls `to_rational()` on epoch values derived from attacker-controlled transaction `since` fields, providing a reachable crash path for any unprivileged transaction sender.

---

### Finding Description

`EpochNumberWithFraction` is a packed 64-bit value encoding `(number, index, length)`. The safe constructor `from_full_value` calls `normalize()`, which rewrites zero-length values to `(number, 0, 1)` to prevent downstream panics. The unsafe constructor `from_full_value_unchecked` skips this step entirely.

The `Unpack` implementation used for all packed-byte deserialization of epoch fields calls `from_full_value_unchecked`: [1](#0-0) 

The `to_rational()` method documents the panic risk explicitly: [2](#0-1) 

The guard `if self.0 == 0` only handles the genesis block (entire packed value is zero). A value with `length == 0` but `number != 0` passes the guard and proceeds to `RationalU256::new(index, 0)`, which panics on division by zero.

The `normalize()` path that would prevent this is only taken by `from_full_value`: [3](#0-2) 

The `SinceVerifier` in transaction verification calls `to_rational()` seven times on epoch values derived from transaction `since` fields: [4](#0-3) 

Additionally, `secondary_block_issuance` and `block_reward` both divide by `self.length()` without a zero guard: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

A panic in the transaction verification pipeline (tx-pool admission or block verification) causes the node process to crash. Any unprivileged actor who can submit a transaction with a crafted `since` field encoding `length == 0, number != 0` can trigger this. This is a remote crash / denial-of-service against any node that processes the transaction.

---

### Likelihood Explanation

The attack requires only submitting a single malformed transaction via the `send_transaction` RPC or P2P relay. No special privileges, keys, or hashpower are needed. The crafted `since` value is a raw u64 field in a transaction input, fully attacker-controlled. The deserialization path unconditionally uses `from_full_value_unchecked`, so no input sanitization occurs before `to_rational()` is reached.

---

### Recommendation

1. Replace `from_full_value_unchecked` with `from_full_value` (which calls `normalize()`) in the `Unpack` and `From` impls in `util/types/src/conversion/blockchain.rs`.
2. Add an explicit `is_well_formed()` check in the `SinceVerifier` before calling `to_rational()` on any epoch derived from a transaction `since` field, returning a verification error rather than panicking.
3. Add zero-length guards in `secondary_block_issuance` and `block_reward` in `util/types/src/core/extras.rs` to return an error rather than panic on division by zero.

---

### Proof of Concept

1. Craft a transaction input with `since` encoding an epoch-type value where `length == 0` and `number == 1` (e.g., packed bits: `number=1 << 0`, `index=0`, `length=0` → raw u64 with the epoch-type flag set and length bits all zero).
2. Submit via `send_transaction` RPC to any CKB node.
3. The node deserializes the `since` field using `from_full_value_unchecked`, producing an `EpochNumberWithFraction` with `length() == 0` and `full_value() != 0`.
4. `SinceVerifier` calls `to_rational()` on this value; the `if self.0 == 0` guard does not fire; `RationalU256::new(0, 0)` panics.
5. Node process crashes. [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** util/types/src/core/extras.rs (L234-242)
```rust
    pub fn block_reward(&self, number: BlockNumber) -> CapacityResult<Capacity> {
        if number >= self.start_number()
            && number < self.start_number() + self.remainder_reward.as_u64()
        {
            self.base_block_reward.safe_add(Capacity::one())
        } else {
            Ok(self.base_block_reward)
        }
    }
```

**File:** util/types/src/core/extras.rs (L255-266)
```rust
    pub fn secondary_block_issuance(
        &self,
        block_number: BlockNumber,
        secondary_epoch_issuance: Capacity,
    ) -> CapacityResult<Capacity> {
        let mut g2 = Capacity::shannons(secondary_epoch_issuance.as_u64() / self.length());
        let remainder = secondary_epoch_issuance.as_u64() % self.length();
        if block_number >= self.start_number() && block_number < self.start_number() + remainder {
            g2 = g2.safe_add(Capacity::one())?;
        }
        Ok(g2)
    }
```

**File:** util/types/src/core/extras.rs (L462-502)
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

**File:** verification/src/transaction_verifier.rs (L1-24)
```rust
use crate::cache::Completed;
use crate::error::TransactionErrorSource;
use crate::{TransactionError, TxVerifyEnv};
use ckb_chain_spec::consensus::Consensus;
use ckb_constant::consensus::ENABLED_SCRIPT_HASH_TYPE;
use ckb_dao::DaoCalculator;
use ckb_dao_utils::DaoError;
use ckb_error::Error;
#[cfg(not(target_family = "wasm"))]
use ckb_script::ChunkCommand;
use ckb_script::{TransactionScriptsVerifier, TransactionState};
use ckb_traits::{
    CellDataProvider, EpochProvider, ExtensionProvider, HeaderFieldsProvider, HeaderProvider,
};
use ckb_types::{
    core::{
        Capacity, Cycle, EpochNumberWithFraction, ScriptHashType, TransactionView, Version,
        cell::{CellMeta, ResolvedTransaction},
    },
    packed::{Byte32, CellOutput},
};
use std::collections::HashSet;
use std::sync::Arc;

```
