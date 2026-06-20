### Title
Hardcoded `WellKnownScriptsOnly` Validator Breaks Non-Mainnet/Testnet Chain Support - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `build_well_known_lock_scripts` and `build_well_known_type_scripts` functions in `rpc/src/module/pool.rs` hardcode script hashes for only `mainnet` and `testnet` chain specs. For every other chain (dev, staging, preview, or any custom chain), both functions return an empty list. This makes the `WellKnownScriptsOnly` output validator non-extensible: on non-mainnet/testnet chains, it silently rejects all transactions whose outputs use well-known scripts such as SUDT (Simple UDT), `anyone_can_pay`, or `cheque`, even though those scripts are valid and widely deployed on those networks.

---

### Finding Description

`PoolRpcImpl::new` calls `build_well_known_lock_scripts` and `build_well_known_type_scripts` with the chain spec name to populate the validator's allowlists: [1](#0-0) 

Both builder functions use a `match` on `chain_spec_name` that only handles two literal values: [2](#0-1) [3](#0-2) 

The wildcard arm `_ => "[]"` means that for `ckb_dev`, `ckb_staging`, `ckb_preview`, or any operator-defined chain, both `well_known_lock_scripts` and `well_known_type_scripts` are empty `Vec`s.

When an RPC caller submits a transaction with `outputs_validator: "well_known_scripts_only"`, the validator runs `validate_lock_script` and `validate_type_script` on every output: [4](#0-3) 

`validate_lock_script` tries `secp256k1_blake160_sighash_all`, then `secp256k1_blake160_multisig_all`, then falls through to `validate_well_known_lock_scripts`. On non-mainnet/testnet chains the last check always fails because the list is empty: [5](#0-4) 

Similarly, `validate_type_script` tries DAO first, then `validate_well_known_type_scripts`, which also always fails on non-mainnet/testnet chains because the list is empty. [6](#0-5) 

The result is that on any non-mainnet/testnet chain, the `WellKnownScriptsOnly` validator cannot accept outputs with `anyone_can_pay`, `cheque`, or SUDT type scripts — scripts that are explicitly documented as "well-known" and are the entire reason the validator exists.

---

### Impact Explanation

**Medium.** Any RPC caller on a non-mainnet/testnet CKB node (dev, staging, preview, or custom chain) who submits a transaction with `outputs_validator: "well_known_scripts_only"` and an output using a well-known script (SUDT, anyone_can_pay, cheque) will receive a `PoolRejectedTransactionByOutputsValidator` error. The validator is supposed to be a safe default that allows exactly these scripts; instead it silently rejects them. This forces users to fall back to `passthrough`, removing all output validation protection and defeating the purpose of the validator. The design is non-extensible: adding a new well-known script to a non-mainnet/testnet chain requires modifying the source code.

---

### Likelihood Explanation

**Medium.** The `ckb_dev`, `ckb_staging`, and `ckb_preview` chain specs are shipped with the node and are actively used by developers and operators. Any operator running a non-mainnet/testnet node who relies on the `WellKnownScriptsOnly` validator (the documented safe default) will encounter this issue as soon as they submit a transaction with a SUDT or anyone_can_pay output. The issue is triggered by a normal, unprivileged RPC call with no special access required.

---

### Recommendation

Replace the hardcoded `match` with a data-driven approach. The well-known script lists should be loaded from the chain spec configuration or from a registry keyed by chain spec name that can be extended without source changes. At minimum, the `PoolRpcImpl::new` constructor already accepts `extra_well_known_lock_scripts` and `extra_well_known_type_scripts` parameters — the chain-spec-specific lists should be populated through that same extensible mechanism rather than through a closed `match` expression. [7](#0-6) 

---

### Proof of Concept

1. Start a CKB node with `ckb_dev` (or any non-mainnet/testnet) chain spec.
2. Obtain a live cell with a secp256k1 lock.
3. Construct a transaction whose output uses an `anyone_can_pay` lock script (code hash `0xd369597ff47f29fbc0d47d2e3775370d1250b85140c670e4718af712983a2354` on mainnet, but deployed at a different hash on dev).
4. Call `send_transaction` with `outputs_validator: "well_known_scripts_only"`.
5. Observe `PoolRejectedTransactionByOutputsValidator` error, even though the script is a documented well-known script.
6. The same transaction succeeds with `outputs_validator: "passthrough"`, confirming the validator is the cause.

Root cause: `build_well_known_lock_scripts("ckb_dev")` returns `[]` because the `match` arm `_ => "[]"` fires for all chains other than mainnet and testnet. [2](#0-1)

### Citations

**File:** rpc/src/module/pool.rs (L479-497)
```rust
    pub fn new(
        shared: Shared,
        mut extra_well_known_lock_scripts: Vec<packed::Script>,
        mut extra_well_known_type_scripts: Vec<packed::Script>,
    ) -> PoolRpcImpl {
        let mut well_known_lock_scripts =
            build_well_known_lock_scripts(shared.consensus().id.as_str());
        let mut well_known_type_scripts =
            build_well_known_type_scripts(shared.consensus().id.as_str());

        well_known_lock_scripts.append(&mut extra_well_known_lock_scripts);
        well_known_type_scripts.append(&mut extra_well_known_type_scripts);

        PoolRpcImpl {
            shared,
            well_known_lock_scripts,
            well_known_type_scripts,
        }
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

**File:** rpc/src/module/pool.rs (L758-776)
```rust
    pub fn validate(&self, tx: &core::TransactionView) -> std::result::Result<(), String> {
        tx.outputs()
            .into_iter()
            .enumerate()
            .try_for_each(|(index, output)| {
                self.validate_lock_script(&output)
                    .and(self.validate_type_script(&output))
                    .map_err(|err| format!("output index: {index}, error: {err:?}"))
            })
    }

    fn validate_lock_script(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        self.validate_secp256k1_blake160_sighash_all(output)
            .or_else(|_| self.validate_secp256k1_blake160_multisig_all(output))
            .or_else(|_| self.validate_well_known_lock_scripts(output))
    }
```

**File:** rpc/src/module/pool.rs (L778-784)
```rust
    fn validate_type_script(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        self.validate_dao(output)
            .or_else(|_| self.validate_well_known_type_scripts(output))
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
