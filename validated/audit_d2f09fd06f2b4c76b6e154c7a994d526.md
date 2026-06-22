### Title
`WellKnownScriptsOnlyValidator` Accepts All-Zero Lock Script Args, Enabling Silent Permanent Fund Loss — (`File: rpc/src/module/pool.rs`)

### Summary
The `WellKnownScriptsOnlyValidator` in `rpc/src/module/pool.rs` validates the structural properties of output lock scripts (hash type, code hash, args length) but does **not** check whether the args field is all-zero bytes. A transaction sender can submit a transaction via `send_transaction` with `outputs_validator: "well_known_scripts_only"` whose output uses the canonical `secp256k1_blake160_sighash_all` lock script but with 20 all-zero bytes as args. The validator accepts it without error, and the CKB capacity in that output is permanently and irrecoverably locked.

### Finding Description
`WellKnownScriptsOnlyValidator::validate_secp256k1_blake160_sighash_all` (and its multisig counterpart) performs three checks on the output lock script:

1. `hash_type` must be `type`
2. `code_hash` must match the secp256k1_blake160_sighash_all system script type hash
3. `args.len()` must equal `BLAKE160_LEN` (20 bytes) [1](#0-0) 

The third check only validates the **length** of the args, not their **content**. The args field encodes the blake160 hash of the recipient's public key. If args is `[0u8; 20]` (all zeros), no known private key can produce a signature that satisfies the lock, making the cell permanently unspendable. The validator returns `Ok(())` for this case: [2](#0-1) 

The same gap exists in `validate_secp256k1_blake160_multisig_all`: [3](#0-2) 

The `check_output_validator` dispatcher invokes this validator when `outputs_validator` is `WellKnownScriptsOnly`: [4](#0-3) 

And `send_transaction` calls it before submitting to the pool: [5](#0-4) 

### Impact Explanation
Any CKB capacity sent to an output whose lock script is `secp256k1_blake160_sighash_all` with all-zero args is permanently burned. The cell can never be consumed because the lock script requires a valid secp256k1 signature over a public key whose blake160 hash equals `0x0000…0000`, which is computationally infeasible to produce. The `WellKnownScriptsOnly` validator is explicitly designed to guard against sending to unspendable outputs, yet it silently passes this case. The loss is irreversible at the protocol level.

### Likelihood Explanation
The `WellKnownScriptsOnly` mode is the named protection mode that wallet developers and integrators are directed to use. A developer constructing a transaction programmatically (e.g., initializing a `Script` builder without setting args, or zeroing a buffer before filling it) can easily produce all-zero args. Because the validator returns no error, the mistake is invisible until the funds are confirmed on-chain and the owner attempts to spend them. The CHANGELOG entry at line 1967 (`#1602: Use all zeros as lock script which can never be unlocked`) confirms the CKB team is aware that all-zero scripts are unspendable, making the absence of this check in the validator a concrete gap. [6](#0-5) 

### Recommendation
In both `validate_secp256k1_blake160_sighash_all` and `validate_secp256k1_blake160_multisig_all`, after confirming `args.len() == BLAKE160_LEN`, add a check that the args bytes are not all zero:

```rust
} else if script.args().raw_data().iter().all(|&b| b == 0) {
    Err(DefaultOutputsValidatorError::ZeroArgs)
} else {
    Ok(())
}
```

Add a corresponding `ZeroArgs` variant to `DefaultOutputsValidatorError` and extend the test suite in `rpc/src/tests/module/pool.rs` to cover this case.

### Proof of Concept
1. Build a transaction output with lock:
   - `hash_type`: `type`
   - `code_hash`: `secp256k1_blake160_sighash_all` type hash (from consensus)
   - `args`: `0x0000000000000000000000000000000000000000` (20 zero bytes)
2. Call `send_transaction` via RPC with `outputs_validator: "well_known_scripts_only"`.
3. Observe: the RPC returns a transaction hash with no error — the validator does not reject the zero args.
4. Once the transaction is committed, the output cell can never be spent, permanently locking the CKB capacity. [2](#0-1) [3](#0-2)

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

**File:** rpc/src/module/pool.rs (L786-805)
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
```

**File:** rpc/src/module/pool.rs (L821-833)
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
        } else {
            Ok(())
```

**File:** CHANGELOG.md (L1967-1967)
```markdown
- #1602: Use all zeros as lock script which can never be unlocked (@driftluo)
```
