### Title
Incomplete Args Validation in `WellKnownScriptsOnlyValidator` Allows Permanent Fund Loss via Malformed Lock Script Args — (`File: rpc/src/module/pool.rs`)

---

### Summary

The `send_transaction` RPC exposes an `outputs_validator` parameter intended to protect users from common mistakes. When set to `"well_known_scripts_only"`, the `WellKnownScriptsOnlyValidator` is supposed to reject outputs with malformed lock/type scripts. However, the helper `is_well_known_script` uses a `starts_with` prefix check against template scripts that have **empty args** (`"args": "0x"`). Because every byte slice starts with an empty slice, this check unconditionally accepts **any args value** for the well-known scripts (ACP, cheque, SUDT). A user who explicitly opts into the "safe" validator can still submit a transaction with wrong args for these scripts, causing permanent, irrecoverable loss of funds — directly analogous to the unvalidated `_btc_addr` in the BitVMBridge `burn` function.

---

### Finding Description

The `send_transaction` RPC in `rpc/src/module/pool.rs` accepts an optional `outputs_validator` parameter. When `null` or `"passthrough"`, no output validation is performed. When `"well_known_scripts_only"`, the `WellKnownScriptsOnlyValidator` is invoked. [1](#0-0) 

For the two built-in system scripts (`secp256k1_blake160_sighash_all` and `secp256k1_blake160_multisig_all`), args length is explicitly checked: [2](#0-1) [3](#0-2) 

However, for the additional well-known scripts (ACP, cheque, SUDT), validation is delegated to `validate_well_known_lock_scripts` and `validate_well_known_type_scripts`, which call `is_well_known_script`: [4](#0-3) 

The template scripts registered for mainnet and testnet all have **empty args** (`"args": "0x"`): [5](#0-4) 

Because `well_known_script.args().as_slice()` is `&[]` (empty), the expression `script.args().as_slice().starts_with(&[])` is **always `true`** regardless of what args the user-supplied script contains. The validator therefore accepts any args for ACP, cheque, and SUDT scripts — including zero bytes, one byte, or any other malformed length. [6](#0-5) 

The `OutputsValidator` enum documents `Passthrough` as the default (no checking) and `WellKnownScriptsOnly` as the safe alternative: [7](#0-6) 

---

### Impact Explanation

A user who explicitly opts into `outputs_validator = "well_known_scripts_only"` — believing it will protect them from common mistakes — can still submit a transaction with malformed args for ACP, cheque, or SUDT scripts:

- **ACP with 0-byte args**: The anyone-can-pay lock with empty args has no owner restriction; any third party can claim the cell's capacity.
- **SUDT with wrong-length args**: The SUDT type script uses args as the owner lock hash (32 bytes). Wrong-length args produce an unspendable cell, permanently locking the tokens.
- **Cheque with wrong-length args**: The cheque script expects exactly 40 bytes (receiver 20 + sender 20). Wrong args make the cell unspendable.

Once committed on-chain, there is no recovery mechanism. The funds are permanently lost or accessible to anyone. This matches the exact impact class of the BitVMBridge `burn` vulnerability.

---

### Likelihood Explanation

The `"well_known_scripts_only"` validator is the explicitly documented "safe" option. Users and wallet developers who read the documentation and opt into it to protect themselves from mistakes receive a false sense of security. The ACP, cheque, and SUDT scripts are widely used on CKB mainnet. A developer building a wallet or dApp who uses `well_known_scripts_only` and constructs an ACP output with wrong args (e.g., omitting the 20-byte lock hash) will have their transaction accepted by the validator and submitted to the pool. The likelihood is **medium**: it requires a user to use the "safe" validator while also making an args error, but the validator's false guarantee makes this scenario more likely than if no validator existed.

---

### Recommendation

In `is_well_known_script`, replace the open-ended `starts_with` prefix check with an exact args-length check for each well-known script type. Specifically:

- For ACP lock: accept only 0, 20, or 21 bytes of args (per RFC-0026).
- For cheque lock: accept only exactly 40 bytes of args.
- For SUDT type: accept only exactly 32 bytes of args.

Alternatively, register the well-known scripts with their minimum required args length in the template, and enforce a minimum-length check rather than a pure prefix check.

---

### Proof of Concept

Using the testnet well-known ACP lock (`code_hash: 0x3419...`), submit via RPC:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "send_transaction",
  "params": [
    {
      "version": "0x0",
      "cell_deps": [...],
      "header_deps": [],
      "inputs": [{ "previous_output": { "tx_hash": "<utxo>", "index": "0x0" }, "since": "0x0" }],
      "outputs": [
        {
          "capacity": "0x2540be400",
          "lock": {
            "code_hash": "0x3419a1c09eb2567f6552ee7a8ecffd64155cffe0f1796e6e61ec088d740c1356",
            "hash_type": "type",
            "args": "0x"
          },
          "type": null
        }
      ],
      "outputs_data": ["0x"],
      "witnesses": [...]
    },
    "well_known_scripts_only"
  ]
}
```

The validator accepts this because `is_well_known_script` checks `[].starts_with([])` → `true`. The transaction is submitted with an ACP cell with empty args (no owner), making the capacity claimable by anyone. The `WellKnownScriptsOnlyValidator.validate` call returns `Ok(())` for this output despite the malformed args. [8](#0-7) [4](#0-3)

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

**File:** rpc/src/module/pool.rs (L758-767)
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
```

**File:** rpc/src/module/pool.rs (L800-801)
```rust
        } else if script.args().len() != BLAKE160_LEN {
            Err(DefaultOutputsValidatorError::ArgsLen)
```

**File:** rpc/src/module/pool.rs (L821-831)
```rust
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

**File:** util/jsonrpc-types/src/pool.rs (L116-121)
```rust
pub enum OutputsValidator {
    /// the default validator, bypass output checking, thus allow any kind of transaction outputs.
    Passthrough,
    /// restricts the lock script and type script usage, see more information on <https://github.com/nervosnetwork/ckb/wiki/Transaction-%C2%BB-Default-Outputs-Validator>
    WellKnownScriptsOnly,
}
```
