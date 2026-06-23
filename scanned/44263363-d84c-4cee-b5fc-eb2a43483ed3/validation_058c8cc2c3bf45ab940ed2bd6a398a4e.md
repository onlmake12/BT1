### Title
Missing Zero-Value Args Check in `WellKnownScriptsOnlyValidator` Allows Permanent Fund Lock — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `WellKnownScriptsOnlyValidator` in `rpc/src/module/pool.rs` validates secp256k1 lock script `args` by checking their byte length (must equal 20 bytes / `BLAKE160_LEN`) but never checks that the args value is non-zero. When a transaction output carries a secp256k1 lock with `args = 0x0000000000000000000000000000000000000000`, the validator silently accepts it. Because no real public key can produce a blake160 preimage of all-zeros, the locked capacity is permanently unspendable — burned on-chain.

---

### Finding Description

`validate_secp256k1_blake160_sighash_all` (and its multisig sibling) perform three checks:

1. `hash_type == Type`
2. `code_hash == secp256k1_blake160_sighash_all_type_hash`
3. `args.len() == BLAKE160_LEN` (20 bytes) [1](#0-0) 

No check is made that `args != [0u8; 20]`. The `args` field in a secp256k1 lock is the blake160 hash of the owner's compressed public key. An all-zero value is not the hash of any known public key; the secp256k1 script will always fail to verify a witness against it, making the cell permanently unspendable.

The same gap exists in `validate_secp256k1_blake160_multisig_all`: [2](#0-1) 

The validator is invoked from `send_transaction` and `test_tx_pool_accept` via `check_output_validator`: [3](#0-2) 

A secondary instance of the same pattern exists in `sanitize_block_assembler_config` in `util/launcher/src/lib.rs`, which accepts a block-assembler `args` of length 20 without checking for all-zeros, meaning a miner who misconfigures `args = "0x0000000000000000000000000000000000000000"` would permanently burn all coinbase rewards: [4](#0-3) 

---

### Impact Explanation

Any capacity sent to an output whose lock is `secp256k1_blake160_sighash_all` with `args = 0x00…00` (20 zero bytes) is permanently unspendable. The secp256k1 on-chain script requires a valid signature from the public key whose blake160 hash equals the args; no such key exists for the all-zero hash. The capacity is effectively burned. The `WellKnownScriptsOnlyValidator` exists precisely to prevent this class of mistake, but its length-only check leaves the zero-args case unguarded.

---

### Likelihood Explanation

The `WellKnownScriptsOnlyValidator` is active whenever:
- The node is configured with `reject_ill_transactions = true` (the default in the integration-test template), **or**
- The RPC caller explicitly passes `outputs_validator = "well_known_scripts_only"` to `send_transaction`.

An unprivileged RPC caller (wallet software, exchange hot-wallet, dApp backend) that trusts the validator to catch bad outputs can construct or receive a transaction with zero args — through a bug in address derivation, a truncated key, or a crafted payload — and submit it. The validator will not reject it. The funds are lost with no recovery path.

---

### Recommendation

Add an explicit non-zero check on `args` inside both `validate_secp256k1_blake160_sighash_all` and `validate_secp256k1_blake160_multisig_all`:

```rust
} else if script.args().raw_data().iter().all(|b| *b == 0) {
    Err(DefaultOutputsValidatorError::ZeroArgs)
} else {
    Ok(())
}
```

Add a corresponding `ZeroArgs` variant to `DefaultOutputsValidatorError`.

Apply the same guard in `sanitize_block_assembler_config` in `util/launcher/src/lib.rs` when checking `block_assembler.args.len() == SECP256K1_BLAKE160_SIGHASH_ALL_ARG_LEN`. [5](#0-4) 

---

### Proof of Concept

```json
// POST to RPC: send_transaction with outputs_validator = "well_known_scripts_only"
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "send_transaction",
  "params": [
    {
      "version": "0x0",
      "cell_deps": [{ "out_point": { "tx_hash": "<secp_dep_tx_hash>", "index": "0x0" }, "dep_type": "dep_group" }],
      "inputs": [{ "previous_output": { "tx_hash": "<funded_cell_tx_hash>", "index": "0x0" }, "since": "0x0" }],
      "outputs": [{
        "capacity": "0x174876e800",
        "lock": {
          "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
          "hash_type": "type",
          "args": "0x0000000000000000000000000000000000000000"
        }
      }],
      "outputs_data": ["0x"],
      "witnesses": ["<valid_witness_for_input>"]
    },
    "well_known_scripts_only"
  ]
}
```

The `WellKnownScriptsOnlyValidator` accepts the transaction (args length = 20 ✓). The output is committed on-chain. The capacity at that output is permanently unspendable because no public key hashes to `0x00…00` under blake160. [6](#0-5)

### Citations

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

**File:** rpc/src/module/pool.rs (L758-805)
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

    fn validate_type_script(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        self.validate_dao(output)
            .or_else(|_| self.validate_well_known_type_scripts(output))
    }

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
```

**File:** rpc/src/module/pool.rs (L807-835)
```rust
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

**File:** util/launcher/src/lib.rs (L101-117)
```rust
            } else if self.args.block_assembler_advanced
                || (block_assembler.hash_type == ScriptHashType::Type
                    && block_assembler.args.len() == SECP256K1_BLAKE160_SIGHASH_ALL_ARG_LEN
                    && check_lock_code_hash(&(&block_assembler.code_hash).into())?)
            {
                if block_assembler.use_binary_version_as_message_prefix {
                    block_assembler.binary_version = self.version.long();
                }
                Some(block_assembler)
            } else {
                warn!(
                    "Miner is disabled because block assembler uses a non-recommended lock format. \
                     Edit ckb.toml or use `ckb run --ba-advanced` for other lock scripts"
                );

                None
            }
```
