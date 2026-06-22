### Title
`ScriptHashTypeVerifier` Fails to Check Type Script Hash Type Against Consensus-Permitted Range — (`File: verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` is documented as verifying that the `ScriptHashType` of **transaction outputs** is within the range permitted by current consensus rules. However, it only inspects `output.lock().hash_type()` and silently ignores `output.type_().hash_type()`. A transaction sender can craft an output whose type script carries a `ScriptHashType` value that is not yet enabled by consensus (e.g., a future `DataN` VM version), and the non-contextual verifier will accept it without complaint.

---

### Finding Description

`ScriptHashTypeVerifier` lives in `NonContextualTransactionVerifier` and is the designated gate for rejecting transactions whose scripts reference VM versions not yet activated by the hard-fork schedule. [1](#0-0) 

The comment at line 785 reads:

> Verify that the ScriptHashType of transaction outputs is within the range permitted by the current consensus rules.

The loop at line 797 iterates over every output and checks only the **lock** script:

```rust
if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
    let val: u8 = hash_type.into();
    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { … }
}
```

The **type** script (`output.type_()`) is never read. An output whose lock script carries an enabled hash type but whose type script carries a disallowed one (e.g., `Data3 = 6` before the corresponding hard fork) passes this verifier entirely.

For comparison, the lower-level structural `check_data` helper correctly validates **both** scripts: [2](#0-1) 

```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

`check_data` only verifies that the raw byte value is structurally valid (even or 1); it does **not** check whether the hash type is currently enabled by consensus. That consensus-level gate is exclusively `ScriptHashTypeVerifier`, which is incomplete.

`ScriptHashTypeVerifier` is wired into `NonContextualTransactionVerifier`: [3](#0-2) 

which is called from the tx-pool admission path (`non_contextual_verify` in `tx-pool/src/util.rs`) and from block verification when `Switch::DISABLE_NON_CONTEXTUAL` is not set. [4](#0-3) 

---

### Impact Explanation

**Tx-pool admission bypass of the non-contextual gate.** Any unprivileged transaction sender can submit a transaction whose output type script uses a `ScriptHashType` value not yet enabled by consensus. The transaction passes `NonContextualTransactionVerifier` and enters the tx-pool. It is only rejected later, during contextual script execution (`ContextualTransactionVerifier::verify`).

**Block-level risk under `assume_valid_targets` / `Switch::DISABLE_SCRIPT`.** When a node is configured with `assume_valid_targets`, blocks up to the target hash are processed with `Switch::DISABLE_SCRIPT`, meaning `ContextualTransactionVerifier` skips script execution: [5](#0-4) [6](#0-5) 

Under that path, the only hash-type gate that runs is `ScriptHashTypeVerifier` — which misses type scripts. A crafted transaction with a future-VM type script can therefore be committed to the chain on nodes using `assume_valid_targets`, while fully-verifying nodes would reject the same block, producing a **consensus split**.

Additionally, a miner who includes such a transaction in a block will have the block rejected by fully-verifying peers, enabling a low-cost block-wasting attack against a miner.

---

### Likelihood Explanation

The entry path requires only the ability to submit a transaction via RPC or P2P relay — no privileged access. Crafting an output with an arbitrary `hash_type` byte is trivial. The `assume_valid_targets` scenario requires the target node to have that feature enabled, which is an operator-level configuration, but the tx-pool admission issue is universally reachable.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's hash type for every output, mirroring the pattern already used in `check_data`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type
        let lock_ht: u8 = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() })?
            .into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&lock_ht) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: lock_ht }.into());
        }

        // Check type script hash type (currently missing)
        if let Some(type_script) = output.type_().to_opt() {
            let type_ht: u8 = TryInto::<ScriptHashType>::try_into(type_script.hash_type())
                .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: type_script.hash_type() })?
                .into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&type_ht) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: type_ht }.into());
            }
        }
    }
    Ok(())
}
```

---

### Proof of Concept

1. Construct a `TransactionView` with one output whose lock script uses `ScriptHashType::Data` (enabled) and whose type script uses `ScriptHashType::Data3` (value `6`, not yet enabled).
2. Call `NonContextualTransactionVerifier::new(&tx, &consensus).verify()`.
3. Observe that it returns `Ok(())` — the disallowed type script hash type is not caught.
4. Confirm that `ENABLED_SCRIPT_HASH_TYPE` does not contain `6`, proving the check was silently skipped.

The root cause is at: [7](#0-6) 

where the loop body reads only `output.lock().hash_type()` and never touches `output.type_()`.

### Citations

**File:** verification/src/transaction_verifier.rs (L61-102)
```rust
/// Context-independent verification checks for transaction
///
/// Basic checks that don't depend on any context
/// Contains:
/// - Check for version
/// - Check for size
/// - Check inputs and output empty
/// - Check for duplicate deps
/// - Check for whether outputs match data
/// - Check whether output lock hash type within enabled range
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

    /// Perform context-independent verification
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
    }
```

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```

**File:** verification/src/transaction_verifier.rs (L785-814)
```rust
// Verify that the ScriptHashType of transaction outputs
// is within the range permitted by the current consensus rules.
pub struct ScriptHashTypeVerifier<'a> {
    transaction: &'a TransactionView,
}

impl<'a> ScriptHashTypeVerifier<'a> {
    pub fn new(transaction: &'a TransactionView) -> Self {
        Self { transaction }
    }

    pub fn verify(&self) -> Result<(), Error> {
        for output in self.transaction.outputs() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(
                        TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                    );
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType {
                    hash_type: output.lock().hash_type(),
                })
                .into());
            }
        }

        Ok(())
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```

**File:** chain/src/verify.rs (L214-238)
```rust
    ) -> VerifyResult {
        let switch: Switch = switch.unwrap_or_else(|| {
            let mut assume_valid_targets = self.shared.assume_valid_targets();
            match *assume_valid_targets {
                Some(ref mut targets) => {
                    //
                    let block_hash: H256 = Into::<H256>::into(BlockView::hash(block));
                    if targets.first().eq(&Some(&block_hash)) {
                        targets.remove(0);
                        info!("CKB reached one assume_valid_target: 0x{}", block_hash);
                    }

                    if targets.is_empty() {
                        assume_valid_targets.take();
                        info!(
                            "CKB reached all assume_valid_targets, will do full verification now"
                        );
                        Switch::NONE
                    } else {
                        Switch::DISABLE_SCRIPT
                    }
                }
                None => Switch::NONE,
            }
        });
```
