### Title
Hardcoded Chain-Specific Script Hashes in `WellKnownScriptsOnly` Validator Cause Incorrect Transaction Rejection on Devnet and Preview Networks - (File: `rpc/src/module/pool.rs`)

### Summary

`build_well_known_lock_scripts` and `build_well_known_type_scripts` in `rpc/src/module/pool.rs` hardcode script hashes only for `mainnet` and `testnet`. For any other supported chain (`ckb_dev`, `ckb_preview`), both functions return an empty list `"[]"`. This causes the `WellKnownScriptsOnly` output validator to incorrectly reject all valid transactions that use anyone-can-pay, cheque, or SUDT scripts on devnet and preview networks, breaking the RPC-level transaction submission guarantee for those networks.

### Finding Description

`build_well_known_lock_scripts` and `build_well_known_type_scripts` are called at node startup inside `PoolRpcImpl::new` to populate the `well_known_lock_scripts` and `well_known_type_scripts` fields used by `WellKnownScriptsOnlyValidator`: [1](#0-0) 

The `match` on `chain_spec_name` only handles `mainnet::CHAIN_SPEC_NAME` and `testnet::CHAIN_SPEC_NAME`. The wildcard arm returns `"[]"`: [2](#0-1) 

The same pattern applies to type scripts: [3](#0-2) 

When `send_transaction` is called with `outputs_validator: "well_known_scripts_only"`, the validator runs `validate_well_known_lock_scripts` and `validate_well_known_type_scripts`. On devnet/preview, both lists are empty, so any output whose lock script is not secp256k1-sighash-all or secp256k1-multisig, or whose type script is not DAO, is rejected with `NotWellKnownLockScript` / `NotWellKnownTypeScript`: [4](#0-3) [5](#0-4) 

The devnet spec (`ckb_dev`) and preview spec (`ckb_preview`) are both supported, first-class network configurations shipped with the node:



The consensus ID is read directly from the chain spec name at runtime: [6](#0-5) 

### Impact Explanation

On devnet and preview networks, any RPC caller who submits a transaction with `outputs_validator: "well_known_scripts_only"` and whose outputs use anyone-can-pay, cheque, or SUDT scripts will receive a `PoolRejectedTransactionByOutputsValidator` error, even though those scripts are perfectly valid on those networks. The validator is silently broken: it provides no protection (empty list) while simultaneously rejecting valid transactions. This breaks the documented contract of the `well_known_scripts_only` validator for all supported networks other than mainnet and testnet.

### Likelihood Explanation

Any RPC caller (developer, tooling, wallet) running against a devnet or preview node and using `outputs_validator: "well_known_scripts_only"` will trigger this. The `extra_well_known_lock_scripts` / `extra_well_known_type_scripts` config fields exist as a workaround but are not documented as required for devnet/preview, and the default generated configs do not set them: [7](#0-6) 

### Recommendation

Replace the hardcoded `match` with a mechanism that derives well-known script hashes from the genesis block for any chain, or at minimum extend the `match` to cover `ckb_dev` and `ckb_preview` with their correct script hashes. The `extra_well_known_lock_scripts` / `extra_well_known_type_scripts` config fields already provide the right extension point; the node should populate them automatically from the chain spec rather than requiring manual operator configuration.

### Proof of Concept

1. Start a CKB node with `ckb init --chain dev` (chain spec name = `ckb_dev`).
2. Deploy an anyone-can-pay cell on the devnet.
3. Call `send_transaction` via RPC with a transaction whose output uses the anyone-can-pay lock script and `outputs_validator: "well_known_scripts_only"`.
4. The node returns `PoolRejectedTransactionByOutputsValidator` because `build_well_known_lock_scripts("ckb_dev")` returns `[]`, so `validate_well_known_lock_scripts` always returns `Err(NotWellKnownLockScript)` for any script not matching secp256k1-sighash-all or secp256k1-multisig.
5. The same transaction submitted with `outputs_validator: "passthrough"` succeeds, confirming the script itself is valid and the rejection is solely due to the hardcoded empty list. [8](#0-7)

### Citations

**File:** rpc/src/module/pool.rs (L484-487)
```rust
        let mut well_known_lock_scripts =
            build_well_known_lock_scripts(shared.consensus().id.as_str());
        let mut well_known_type_scripts =
            build_well_known_type_scripts(shared.consensus().id.as_str());
```

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

**File:** rpc/src/module/pool.rs (L534-571)
```rust
fn build_well_known_lock_scripts(chain_spec_name: &str) -> Vec<packed::Script> {
    serde_json::from_str::<Vec<Script>>(
    match chain_spec_name {
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
}
```

**File:** rpc/src/module/pool.rs (L576-603)
```rust
fn build_well_known_type_scripts(chain_spec_name: &str) -> Vec<packed::Script> {
    serde_json::from_str::<Vec<Script>>(
    match chain_spec_name {
        mainnet::CHAIN_SPEC_NAME => {
            r#"
            [
                {
                    "code_hash": "0x5e7a36a77e68eecc013dfa2fe6a23f3b6c344b04005808694ae6dd45eea4cfd5",
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
                    "code_hash": "0xc5e5dcf215925f7ef4dfaf5f4b4f105bc321c02776d6e7d52a1db3fcd9d011a4",
                    "hash_type": "type",
                    "args": "0x"
                }
            ]
            "#
        }
        _ => "[]"
    }).expect("checked json str").into_iter().map(Into::into).collect()
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

**File:** rpc/src/module/pool.rs (L881-898)
```rust
    fn validate_well_known_type_scripts(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        if let Some(script) = output.type_().to_opt() {
            if self
                .well_known_type_scripts
                .iter()
                .any(|well_known_script| is_well_known_script(&script, well_known_script))
            {
                Ok(())
            } else {
                Err(DefaultOutputsValidatorError::NotWellKnownTypeScript)
            }
        } else {
            Ok(())
        }
    }
```

**File:** util/app-config/src/configs/rpc.rs (L55-61)
```rust
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```
