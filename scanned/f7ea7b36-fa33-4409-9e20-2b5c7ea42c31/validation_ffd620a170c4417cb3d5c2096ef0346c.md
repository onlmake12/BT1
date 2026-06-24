Audit Report

## Title
`WellKnownScriptsOnly` Args Validation Bypass via `starts_with(&[])` Always-True — (`rpc/src/module/pool.rs`)

## Summary
`is_well_known_script` uses Rust's `starts_with` to validate that a submitted script's args match the well-known script's args. Because every well-known lock and type script on both mainnet and testnet is hardcoded with `"args": "0x"` (empty bytes), `well_known_script.args().as_slice()` is always `&[]`. In Rust, `any_slice.starts_with(&[])` is unconditionally `true`, making the args check a complete no-op. Any script sharing a well-known `code_hash` and `hash_type` passes the validator regardless of its args content.

## Finding Description
`is_well_known_script` at `rpc/src/module/pool.rs` lines 912–919:

```rust
fn is_well_known_script(script: &packed::Script, well_known_script: &packed::Script) -> bool {
    script.hash_type() == well_known_script.hash_type()
        && script.code_hash() == well_known_script.code_hash()
        && script
            .args()
            .as_slice()
            .starts_with(well_known_script.args().as_slice())
}
``` [1](#0-0) 

All well-known lock scripts (mainnet and testnet) are built by `build_well_known_lock_scripts` with `"args": "0x"`: [2](#0-1) 

Because `well_known_script.args().as_slice()` always returns `&[]`, the third condition becomes `script.args().as_slice().starts_with(&[])`, which is `true` for every byte slice in Rust — including a 100-byte attacker-crafted args field. The args dimension of the check is never enforced.

The call chain is:
1. `check_output_validator` (lines 499–526) dispatches to `WellKnownScriptsOnlyValidator::validate`
2. `validate` calls `validate_lock_script` per output (lines 762–766)
3. `validate_lock_script` falls through to `validate_well_known_lock_scripts` after both secp256k1 checks fail on a mismatched `code_hash` (lines 769–775)
4. `validate_well_known_lock_scripts` calls `is_well_known_script` and returns `Ok(())` on any match (lines 837–851) [3](#0-2) [4](#0-3) 

## Impact Explanation
Wallets and exchanges call `send_transaction` with `OutputsValidator::WellKnownScriptsOnly` specifically to guarantee that output lock scripts conform to known-safe semantics — including valid args lengths and formats. The broken args check means any script carrying a well-known `code_hash`+`hash_type` with **arbitrary args** passes the validator. This enables an attacker to direct funds to:
- An unspendable address (malformed args that no valid unlock witness can satisfy), permanently destroying CKB.
- An attacker-controlled address (args crafted to be spendable by the attacker under the script's logic, e.g., substituting the attacker's lock hash in an anyone-can-pay script).

This matches the allowed bounty impact: **"Vulnerabilities which could easily damage CKB economy" (Critical)**.

## Likelihood Explanation
The `anyone_can_pay` and `cheque` scripts are widely deployed. Any unprivileged caller can invoke `send_transaction` with `WellKnownScriptsOnly`. An attacker needs only to supply a payment address whose lock script uses one of the well-known `code_hash` values with non-standard args — a trivial construction. No special privileges, leaked keys, or victim mistakes are required beyond the victim wallet/exchange using the validator as intended.

## Recommendation
Replace `starts_with` with exact equality on args:

```rust
fn is_well_known_script(script: &packed::Script, well_known_script: &packed::Script) -> bool {
    script.hash_type() == well_known_script.hash_type()
        && script.code_hash() == well_known_script.code_hash()
        && script.args() == well_known_script.args()
}
```

If prefix-matching is intentionally desired for future well-known scripts with non-empty args, add a guard `!well_known_script.args().is_empty()` before the `starts_with` call to prevent the empty-prefix bypass.

## Proof of Concept

```rust
use ckb_types::{packed, prelude::*};

#[test]
fn prove_args_bypass() {
    // well-known script with empty args, as hardcoded in build_well_known_lock_scripts
    let well_known = packed::Script::new_builder()
        .code_hash([0xd3u8; 32].pack())
        .hash_type(1u8.into())
        .args(packed::Bytes::default()) // "args": "0x"
        .build();

    // attacker script: same code_hash/hash_type, 100 arbitrary bytes as args
    let attacker_script = packed::Script::new_builder()
        .code_hash([0xd3u8; 32].pack())
        .hash_type(1u8.into())
        .args(vec![0xffu8; 100].pack())
        .build();

    // starts_with(&[]) is always true — bypass confirmed
    assert!(
        is_well_known_script(&attacker_script, &well_known),
        "BUG: args are never validated"
    );
}
```

`well_known_script.args().as_slice()` returns `&[]`; `attacker_script.args().as_slice().starts_with(&[])` is unconditionally `true`. The test passes, confirming the validator accepts the attacker script.

### Citations

**File:** rpc/src/module/pool.rs (L537-570)
```rust
        mainnet::CHAIN_SPEC_NAME => {
            r#"
            [
                {
                    "code_hash": "0xd369597ff47f29fbc0d47d2e3775370d1250b85140c670e4718af712983a2354",
                    "hash_type": "type",
                    "args": "0x"
                },
                {
                    "code_hash": "0xe4d4ecc6e5f9a059bf2f7a82cca292083aebc0c421566a52484fe2ec51a9fb0c",
                    "hash_type": "type",
                    "args": "0x"
                }
            ]
            "#
        }
        testnet::CHAIN_SPEC_NAME => {
            r#"
            [
                {
                    "code_hash": "0x3419a1c09eb2567f6552ee7a8ecffd64155cffe0f1796e6e61ec088d740c1356",
                    "hash_type": "type",
                    "args": "0x"
                },
                {
                    "code_hash": "0x60d5f39efce409c587cb9ea359cefdead650ca128f0bd9cb3855348f98c70d5b",
                    "hash_type": "type",
                    "args": "0x"
                }
            ]
            "#
        }
        _ => "[]"
    }).expect("checked json str").into_iter().map(Into::into).collect()
```

**File:** rpc/src/module/pool.rs (L769-776)
```rust
    fn validate_lock_script(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        self.validate_secp256k1_blake160_sighash_all(output)
            .or_else(|_| self.validate_secp256k1_blake160_multisig_all(output))
            .or_else(|_| self.validate_well_known_lock_scripts(output))
    }
```

**File:** rpc/src/module/pool.rs (L837-851)
```rust
    fn validate_well_known_lock_scripts(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        let script = output.lock();
        if self
            .well_known_lock_scripts
            .iter()
            .any(|well_known_script| is_well_known_script(&script, well_known_script))
        {
            Ok(())
        } else {
            Err(DefaultOutputsValidatorError::NotWellKnownLockScript)
        }
    }
```

**File:** rpc/src/module/pool.rs (L912-919)
```rust
fn is_well_known_script(script: &packed::Script, well_known_script: &packed::Script) -> bool {
    script.hash_type() == well_known_script.hash_type()
        && script.code_hash() == well_known_script.code_hash()
        && script
            .args()
            .as_slice()
            .starts_with(well_known_script.args().as_slice())
}
```
