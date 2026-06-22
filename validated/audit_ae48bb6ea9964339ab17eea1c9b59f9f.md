### Title
Custom Secp256k1 Signature Range Validation Reimplemented Instead of Using Library Primitives — (`util/crypto/src/secp/signature.rs`)

### Summary
CKB reimplements secp256k1 signature component range validation in a custom `Signature::is_valid()` function using its own `H256` fixed-hash comparison operators, rather than delegating to the battle-tested `secp256k1` crate's own validation. This custom pre-filter is used in the network alert verifier to silently discard signatures before the m-of-n threshold check, creating a divergence risk between CKB's custom range logic and the underlying library's semantics.

### Finding Description

`util/crypto/src/secp/signature.rs` defines a custom `Signature` wrapper around the secp256k1 recoverable signature format and implements its own validity check:

```rust
const N: H256 = h256!("0xffffffff_ffffffff_ffffffff_fffffffe_baaedce6_af48a03b_bfd25e8c_d0364141");
const ONE: H256 = h256!("0x1");

pub fn is_valid(&self) -> bool {
    let h_r = match H256::from_slice(self.r()) { Ok(h_r) => h_r, Err(_) => return false };
    let h_s = match H256::from_slice(self.s()) { Ok(h_s) => h_s, Err(_) => return false };
    self.v() <= 1 && h_r < N && h_r >= ONE && h_s < N && h_s >= ONE
}
``` [1](#0-0) 

This manually encodes the secp256k1 curve order `N` as a `H256` constant and uses `H256`'s `PartialOrd` implementation (from `util/fixed-hash/core/src/std_cmp.rs`) to perform big-integer range comparisons on the raw `r` and `s` byte slices. The `secp256k1` crate already enforces these exact constraints internally inside `RecoverableSignature::from_compact()` and `verify_ecdsa()`.

This custom `is_valid()` is then used as a **silent pre-filter** in the network alert verifier:

```rust
|sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
    Ok(sig) => {
        if sig.is_valid() { Some(sig) } else {
            debug!("invalid signature: {:?}", sig);
            None  // silently dropped
        }
    }
    ...
}
``` [2](#0-1) 

Signatures that fail `is_valid()` are silently dropped before being passed to `verify_m_of_n`. [3](#0-2) 

Additionally, the `From<Vec<u8>> for Signature` conversion is implemented without length validation:

```rust
impl From<Vec<u8>> for Signature {
    fn from(sig: Vec<u8>) -> Self {
        let mut data = [0; 65];
        data[0..65].copy_from_slice(sig.as_slice()); // panics if sig.len() != 65
        Signature(data)
    }
}
``` [4](#0-3) 

This is a custom reimplementation that bypasses the safe `from_slice` path (which returns a `Result`) and will panic on any non-65-byte input.

### Impact Explanation

The `is_valid()` pre-filter governs which signatures reach the `verify_m_of_n` threshold check in the network alert system. If the `H256` comparison operators in `util/fixed-hash` have different byte-order semantics than the secp256k1 curve order (e.g., little-endian vs. big-endian), the range check `h_r < N` would be evaluated against an incorrect bound. This could cause:

- **False negatives**: Valid signatures with `r` or `s` values near the curve order boundary are silently dropped, preventing a legitimate network alert from reaching the required threshold even when enough valid Nervos Foundation signatures are present.
- **False positives**: Signatures with out-of-range `r`/`s` values pass the pre-filter (though the secp256k1 library provides a second layer of defense in `recover()`).

The `From<Vec<u8>>` panic is a node-crash vector if called with attacker-controlled data of wrong length.

### Likelihood Explanation

The `H256` type is a fixed-size hash type (not a fixed-size integer type), and its comparison is likely lexicographic big-endian, which would be correct for secp256k1 values. However, the divergence risk is real: any future change to `util/fixed-hash`'s comparison semantics, or a subtle endianness mismatch in the `h256!` macro, would silently corrupt the pre-filter without any test catching it, since the secp256k1 library's own validation masks the error in the happy path. The `From<Vec<u8>>` panic is low-likelihood in practice but is an unguarded custom reimplementation.

### Recommendation

- Remove `Signature::is_valid()` entirely. The `secp256k1` crate already validates `r`, `s`, and `v` ranges inside `RecoverableSignature::from_compact()` and `recover_ecdsa()`. The pre-filter is redundant and introduces a maintenance-divergence risk.
- Replace the `From<Vec<u8>> for Signature` implementation with one that delegates to `from_slice` and returns a `Result`, or remove it in favor of the existing safe `from_slice` method.
- In `verify_signatures`, rely solely on the `secp256k1` library's error returns from `recover()` to filter invalid signatures, rather than a custom pre-filter.

### Proof of Concept

1. The custom range check in `is_valid()` at `util/crypto/src/secp/signature.rs:63–78` manually encodes the secp256k1 curve order as an `H256` constant and uses `H256::PartialOrd` for comparison — a reimplementation of logic already present in the `secp256k1` crate. [5](#0-4) 

2. This pre-filter is the sole gate in `util/network-alert/src/verifier.rs:40–54` before signatures reach `verify_m_of_n`. Any divergence in `H256` comparison semantics silently drops signatures without any error propagation. [6](#0-5) 

3. The `secp256k1` crate's `RecoverableSignature::from_compact()` (called inside `recover()` at `util/crypto/src/secp/signature.rs:81–87`) already enforces the same `r`, `s` range constraints, making the custom `is_valid()` pre-filter redundant and a maintenance liability. [7](#0-6) 

4. The `From<Vec<u8>> for Signature` at `util/crypto/src/secp/signature.rs:139–145` will panic on any input not exactly 65 bytes, unlike the safe `from_slice` method at lines 53–60 which returns a `Result`. [4](#0-3)

### Citations

**File:** util/crypto/src/secp/signature.rs (L16-78)
```rust
const N: H256 = h256!("0xffffffff_ffffffff_ffffffff_fffffffe_baaedce6_af48a03b_bfd25e8c_d0364141");
const ONE: H256 = h256!("0x1");

impl Signature {
    /// Get a slice into the 'r' portion of the data.
    pub fn r(&self) -> &[u8] {
        &self.0[0..32]
    }

    /// Get a slice into the 's' portion of the data.
    pub fn s(&self) -> &[u8] {
        &self.0[32..64]
    }

    /// Get the recovery id.
    pub fn v(&self) -> u8 {
        self.0[64]
    }

    /// Construct a new Signature from compact serialize slice and rec_id
    pub fn from_compact(rec_id: RecoveryId, ret: [u8; 64]) -> Self {
        let mut data = [0; 65];
        data[0..64].copy_from_slice(&ret[0..64]);
        data[64] = Into::<i32>::into(rec_id) as u8;
        Signature(data)
    }

    /// Construct a new Signature from rsv.
    pub fn from_rsv(r: &H256, s: &H256, v: u8) -> Self {
        let mut sig = [0u8; 65];
        sig[0..32].copy_from_slice(r.as_bytes());
        sig[32..64].copy_from_slice(s.as_bytes());
        sig[64] = v;
        Signature(sig)
    }

    /// Construct a new Signature from slice.
    pub fn from_slice(data: &[u8]) -> Result<Self, Error> {
        if data.len() != 65 {
            return Err(Error::InvalidSignature);
        }
        let mut sig = [0u8; 65];
        sig[..].copy_from_slice(data);
        Ok(Signature(sig))
    }

    /// Check if each component of the signature is in range.
    pub fn is_valid(&self) -> bool {
        let h_r = match H256::from_slice(self.r()) {
            Ok(h_r) => h_r,
            Err(_) => {
                return false;
            }
        };

        let h_s = match H256::from_slice(self.s()) {
            Ok(h_s) => h_s,
            Err(_) => {
                return false;
            }
        };
        self.v() <= 1 && h_r < N && h_r >= ONE && h_s < N && h_s >= ONE
    }
```

**File:** util/crypto/src/secp/signature.rs (L81-87)
```rust
    pub fn to_recoverable(&self) -> Result<RecoverableSignature, Error> {
        let recovery_id = RecoveryId::try_from(i32::from(self.0[64]))?;
        Ok(RecoverableSignature::from_compact(
            &self.0[0..64],
            recovery_id,
        )?)
    }
```

**File:** util/crypto/src/secp/signature.rs (L139-145)
```rust
impl From<Vec<u8>> for Signature {
    fn from(sig: Vec<u8>) -> Self {
        let mut data = [0; 65];
        data[0..65].copy_from_slice(sig.as_slice());
        Signature(data)
    }
}
```

**File:** util/network-alert/src/verifier.rs (L36-55)
```rust
        let signatures: Vec<Signature> = alert
            .signatures()
            .into_iter()
            .filter_map(
                |sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
                    Ok(sig) => {
                        if sig.is_valid() {
                            Some(sig)
                        } else {
                            debug!("invalid signature: {:?}", sig);
                            None
                        }
                    }
                    Err(err) => {
                        debug!("signature error: {}", err);
                        None
                    }
                },
            )
            .collect();
```

**File:** util/network-alert/src/verifier.rs (L56-63)
```rust
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
```
