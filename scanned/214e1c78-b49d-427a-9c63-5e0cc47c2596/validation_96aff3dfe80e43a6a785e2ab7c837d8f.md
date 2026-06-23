### Title
Incomplete Lock Script Args Validation in `WellKnownScriptsOnlyValidator` Allows Malformed Destination Args to Pass — (`File: rpc/src/module/pool.rs`)

### Summary

The `WellKnownScriptsOnlyValidator` in `rpc/src/module/pool.rs` does not validate the content or format of lock script `args` for well-known scripts (anyone-can-pay, cheque). The helper `is_well_known_script` uses a prefix match against a stored template that has empty `args` (`"args": "0x"`), meaning any `args` value passes validation. An RPC caller submitting a transaction via `send_transaction` with `outputs_validator=well_known_scripts_only` can provide arbitrarily malformed `args` for these scripts and receive no warning. If the `args` are malformed, the resulting cell may be permanently unspendable, locking the CKB.

### Finding Description

The `WellKnownScriptsOnlyValidator` is the CKB node's RPC-layer protection against common output mistakes. It is invoked when a caller passes `outputs_validator=well_known_scripts_only` to `send_transaction` or `test_tx_pool_accept`. [1](#0-0) 

For the two built-in secp256k1 scripts, the validator enforces strict `args` length constraints (exactly 20 bytes, or 20+8 bytes with a valid `since` field): [2](#0-1) 

However, for all other well-known scripts (anyone-can-pay, cheque on mainnet/testnet), validation is delegated to `validate_well_known_lock_scripts`, which calls `is_well_known_script`: [3](#0-2) 

The `is_well_known_script` function performs a `starts_with` check against the stored template's `args` slice: [4](#0-3) 

The well-known lock scripts are registered with `"args": "0x"` (empty bytes): [5](#0-4) 

Because `starts_with(empty_slice)` is always `true`, any `args` value — including zero bytes, 1000 bytes of garbage, or a truncated hash — passes validation for these scripts. The validator returns `Ok(())` without inspecting the `args` content at all.

### Impact Explanation

In CKB, the lock script `args` field is the functional equivalent of a destination address. For the cheque script, `args` must encode exactly 20 bytes of receiver lock hash followed by 20 bytes of sender lock hash. For anyone-can-pay, `args` must be 0, 1, or 17 bytes encoding minimum payment thresholds. If `args` are malformed (wrong length, wrong encoding), the on-chain script will reject all spending attempts, permanently locking the CKB capacity in that cell. The `WellKnownScriptsOnlyValidator` is the only RPC-layer guard against this class of mistake, but it provides no protection for well-known scripts beyond checking `code_hash` and `hash_type`. A user who relies on `well_known_scripts_only` to catch output mistakes receives no warning when they provide malformed `args` for these scripts.

### Likelihood Explanation

The `send_transaction` RPC is reachable by any unprivileged RPC caller. The `outputs_validator` parameter is optional and defaults to `passthrough`, but users and wallets that explicitly set `well_known_scripts_only` to protect themselves are the ones most likely to be misled by the incomplete validation. A copy-paste error, wrong hex encoding, or off-by-one in `args` length is a realistic mistake. The validator's partial coverage (strict for secp256k1 scripts, absent for well-known scripts) creates a false sense of security.

### Recommendation

For each well-known lock script registered in `build_well_known_lock_scripts`, define the expected `args` length range (or exact length) alongside the script template. In `validate_well_known_lock_scripts`, after the `is_well_known_script` prefix check passes, additionally verify that `script.args().len()` falls within the expected range for that specific well-known script. For example, cheque requires exactly 40 bytes; anyone-can-pay requires 0, 1, or 17 bytes. This mirrors the existing strict validation applied to `secp256k1_blake160_sighash_all` at line 800. [6](#0-5) 

### Proof of Concept

1. Obtain the anyone-can-pay `code_hash` for mainnet (`0xd369597ff47f29fbc0d47d2e3775370d1250b85140c670e4718af712983a2354`) and `hash_type=type`.
2. Submit via `send_transaction` with `outputs_validator=well_known_scripts_only`, setting the output lock to that code_hash/hash_type but with `args=0x` followed by 100 random bytes.
3. `validate_well_known_lock_scripts` calls `is_well_known_script`, which evaluates `script.args().as_slice().starts_with(&[])` → `true`.
4. The validator returns `Ok(())` and the transaction enters the pool.
5. The transaction is committed. The cell's lock script has malformed `args`; the anyone-can-pay script rejects all spending attempts, permanently locking the CKB capacity. [4](#0-3) [7](#0-6)

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

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }
```

**File:** rpc/src/module/pool.rs (L786-835)
```rust
    fn validate_secp256k1_blake160_sighash_all(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        let script = output.lock();
        if !script.is_hash_type_type() {
            Err(DefaultOutputsValidatorError::HashType)
        } else if script.code_hash()
            != self
                .consensus
                .secp256k1_blake160_sighash_all_type_hash()
                .expect("No secp256k1_blake160_sighash_all system cell")
        {
            Err(DefaultOutputsValidatorError::CodeHash)
        } else if script.args().len() != BLAKE160_LEN {
            Err(DefaultOutputsValidatorError::ArgsLen)
        } else {
            Ok(())
        }
    }

    fn validate_secp256k1_blake160_multisig_all(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        let script = output.lock();
        if !script.is_hash_type_type() {
            Err(DefaultOutputsValidatorError::HashType)
        } else if script.code_hash()
            != self
                .consensus
                .secp256k1_blake160_multisig_all_type_hash()
                .expect("No secp256k1_blake160_multisig_all system cell")
        {
            Err(DefaultOutputsValidatorError::CodeHash)
        } else if script.args().len() != BLAKE160_LEN {
            if script.args().len() == BLAKE160_LEN + SINCE_LEN {
                if extract_since_from_secp256k1_blake160_multisig_all_args(&script).flags_is_valid()
                {
                    Ok(())
                } else {
                    Err(DefaultOutputsValidatorError::ArgsSince)
                }
            } else {
                Err(DefaultOutputsValidatorError::ArgsLen)
            }
        } else {
            Ok(())
        }
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
