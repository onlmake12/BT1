### Title
`WellKnownScriptsOnly` Args Validation Bypass via `starts_with(&[])` Always-True — (`rpc/src/module/pool.rs`)

---

### Summary

`is_well_known_script` uses Rust's `starts_with` to compare a submitted script's args against the well-known script's args. Because every well-known script on mainnet and testnet is defined with `"args": "0x"` (empty), `well_known_script.args().as_slice()` is always `&[]`. In Rust, `any_slice.starts_with(&[])` is unconditionally `true`. The args check is therefore a no-op: any script with a matching `code_hash` and `hash_type` passes, regardless of what args it carries.

---

### Finding Description

`is_well_known_script` is defined as: [1](#0-0) 

The third condition evaluates `script.args().as_slice().starts_with(well_known_script.args().as_slice())`. Every well-known lock script and type script for both mainnet and testnet is hardcoded with `"args": "0x"`: [2](#0-1) 

Because `well_known_script.args().as_slice()` is always `&[]`, the `starts_with` call reduces to `script.args().as_slice().starts_with(&[])`, which is `true` for every possible byte slice in Rust. The args field is never actually validated.

`validate_well_known_lock_scripts` calls `is_well_known_script` and returns `Ok(())` on any match: [3](#0-2) 

`validate_lock_script` falls through to `validate_well_known_lock_scripts` after the two secp256k1 checks fail (they fail because the code_hash won't match secp256k1): [4](#0-3) 

---

### Impact Explanation

The `WellKnownScriptsOnly` validator is used by wallets and exchanges that call `send_transaction` with `OutputsValidator::WellKnownScriptsOnly` to ensure they only broadcast transactions whose outputs use scripts with known-safe semantics. The validator is supposed to reject outputs whose lock scripts have non-standard args (e.g., a 100-byte args field on an anyone-can-pay script is not a valid anyone-can-pay address).

Because the args check is a no-op, any output whose lock script carries the anyone-can-pay or cheque `code_hash`+`hash_type` with **arbitrary args** passes the validator. A wallet or exchange relying on this validator would accept and broadcast such a transaction, potentially sending funds to:

- An unspendable address (malformed args that no valid unlock witness can satisfy), permanently destroying the CKB.
- An attacker-controlled address (args crafted to be spendable by the attacker under the script's logic).

The `check_output_validator` path is reachable by any unprivileged RPC caller: [5](#0-4) 

---

### Likelihood Explanation

The well-known scripts (`anyone_can_pay`, `cheque`, Simple UDT) are widely used. Wallets and exchanges that integrate CKB and use `WellKnownScriptsOnly` as a safety guard are the direct targets. An attacker only needs to supply a payment address whose lock script uses one of the well-known `code_hash` values with non-standard args. The validator will accept it without any further scrutiny.

---

### Recommendation

Replace the `starts_with` comparison with an exact equality check on args:

```rust
fn is_well_known_script(script: &packed::Script, well_known_script: &packed::Script) -> bool {
    script.hash_type() == well_known_script.hash_type()
        && script.code_hash() == well_known_script.code_hash()
        && script.args() == well_known_script.args()
}
```

If prefix-matching is intentionally desired for some future use case where well-known scripts carry non-empty args, the guard `!well_known_script.args().is_empty()` must be added before the `starts_with` call to prevent the empty-prefix bypass.

---

### Proof of Concept

```rust
use ckb_types::{packed, prelude::*};

fn is_well_known_script(script: &packed::Script, well_known_script: &packed::Script) -> bool {
    script.hash_type() == well_known_script.hash_type()
        && script.code_hash() == well_known_script.code_hash()
        && script
            .args()
            .as_slice()
            .starts_with(well_known_script.args().as_slice())
}

#[test]
fn prove_args_bypass() {
    // well-known script with empty args (as defined in build_well_known_lock_scripts)
    let well_known = packed::Script::new_builder()
        .code_hash([0xd3u8; 32].pack()) // anyone-can-pay mainnet code_hash
        .hash_type(1u8.into())           // type
        .args(packed::Bytes::default())  // "args": "0x"
        .build();

    // attacker script: same code_hash/hash_type, but 100 arbitrary bytes as args
    let attacker_script = packed::Script::new_builder()
        .code_hash([0xd3u8; 32].pack())
        .hash_type(1u8.into())
        .args(vec![0xffu8; 100].pack())  // 100-byte non-standard args
        .build();

    // Invariant: should be false. Actual result: true — bypass confirmed.
    assert!(
        is_well_known_script(&attacker_script, &well_known),
        "BUG: starts_with(&[]) is always true; args are never validated"
    );
}
```

The empty `well_known_script.args().as_slice()` (`&[]`) makes `starts_with` trivially true for any attacker-supplied args, confirming the invariant violation described in the question.

### Citations

**File:** rpc/src/module/pool.rs (L499-526)
```rust
    fn check_output_validator(
        &self,
        outputs_validator: Option<OutputsValidator>,
        tx: &TransactionView,
    ) -> Result<()> {
        if let Err(e) = match outputs_validator {
            None | Some(OutputsValidator::Passthrough) => Ok(()),
            Some(OutputsValidator::WellKnownScriptsOnly) => WellKnownScriptsOnlyValidator::new(
                self.shared.consensus(),
                &self.well_known_lock_scripts,
                &self.well_known_type_scripts,
            )
            .validate(tx),
        } {
            return Err(RPCError::custom_with_data(
                RPCError::PoolRejectedTransactionByOutputsValidator,
                format!(
                    "The transaction is rejected by OutputsValidator set in params[1]: {}. \
                    Please check the related information in https://github.com/nervosnetwork/ckb/wiki/Transaction-%C2%BB-Default-Outputs-Validator",
                    outputs_validator
                        .unwrap_or(OutputsValidator::WellKnownScriptsOnly)
                        .json_display()
                ),
                e,
            ));
        }
        Ok(())
    }
```

**File:** rpc/src/module/pool.rs (L538-570)
```rust
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
