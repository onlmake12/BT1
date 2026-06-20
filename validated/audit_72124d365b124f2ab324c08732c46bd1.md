### Title
`ScriptHashTypeVerifier` Fails to Validate Type Script Hash Types in Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

The `ScriptHashTypeVerifier` in CKB's non-contextual transaction verification pipeline only validates the **lock script** hash type of each output against the consensus-permitted set (`ENABLED_SCRIPT_HASH_TYPE`). It completely omits the same check for **type scripts**. Any unprivileged transaction sender can submit a transaction whose output carries a type script with a hash type that is not yet enabled by consensus, bypassing the guard entirely and committing such a cell to the canonical chain.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` iterates over every output in a transaction and performs the following check:

```rust
// verification/src/transaction_verifier.rs  L797-L814
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
``` [1](#0-0) 

The loop calls `output.lock().hash_type()` exclusively. It never calls `output.type_().to_opt()` to retrieve and validate the type script's hash type. A transaction output that carries a type script with hash type `0x04` (or any future/unsupported byte value absent from `ENABLED_SCRIPT_HASH_TYPE`) will pass this verifier without error.

`ScriptHashTypeVerifier` is composed into `NonContextualTransactionVerifier`, which is the first gate applied to every incoming transaction — both from the P2P relay path and from the local RPC `send_transaction` endpoint:

```rust
// verification/src/transaction_verifier.rs  L94-L102
pub fn verify(&self) -> Result<(), Error> {
    self.version.verify()?;
    self.size.verify()?;
    self.empty.verify()?;
    self.duplicate_deps.verify()?;
    self.outputs_data_verifier.verify()?;
    self.script_hash_type.verify()?;   // ← only checks lock scripts
    Ok(())
}
``` [2](#0-1) 

No subsequent contextual verifier re-applies the hash-type range check to type scripts before the transaction is committed to a block.

---

### Impact Explanation

A cell committed to the chain with an unsupported type script hash type has two concrete consequences:

1. **Permanently unspendable cell / locked funds.** When the cell is later consumed as an input, the CKB-VM must execute its type script. The unsupported hash type causes script resolution to fail, making the cell unspendable until (and unless) a future hardfork enables that hash type. Any CKB capacity locked in such a cell is effectively frozen.

2. **Consensus-rule bypass.** The `ENABLED_SCRIPT_HASH_TYPE` guard exists precisely to prevent premature use of hash types that the network has not yet agreed to support. Bypassing it for type scripts undermines the hardfork gating mechanism: a cell with a future hash type in its type script can be created today, creating a dependency on a hardfork that may never arrive or may arrive with different semantics.

---

### Likelihood Explanation

The entry path requires no privilege. Any actor who can submit a transaction — via the P2P relay (`submit_remote_tx`), the RPC `send_transaction` call, or the local `process_tx` path — can craft an output with an arbitrary type script hash type byte. The non-contextual verifier is the only gate that would catch this, and it is incomplete. The transaction will pass `pre_check`, `verify_rtx`, and `submit_entry` without rejection. [3](#0-2) [4](#0-3) 

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also inspect the type script of each output, mirroring the existing lock-script check:

```rust
for output in self.transaction.outputs() {
    // existing lock script check …

    // add: type script check
    if let Some(type_script) = output.type_().to_opt() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(
                    TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                );
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: type_script.hash_type(),
            })
            .into());
        }
    }
}
```

A unit test should be added that constructs a transaction whose output carries a type script with a hash type byte outside `ENABLED_SCRIPT_HASH_TYPE` and asserts that `ScriptHashTypeVerifier::verify()` returns `Err(TransactionError::ScriptHashTypeNotPermitted)`. This is precisely the class of regression that the external report's recommendation (unit and fork tests) would have surfaced.

---

### Proof of Concept

1. Construct a `TransactionView` with one output whose **type script** `hash_type` byte is set to a value not present in `ENABLED_SCRIPT_HASH_TYPE` (e.g., `0x04`), while the **lock script** uses a valid hash type (e.g., `0x01` — `Type`).
2. Instantiate `NonContextualTransactionVerifier::new(&tx, &consensus)` and call `.verify()`.
3. Observe that `verify()` returns `Ok(())` — the invalid type script hash type is not detected.
4. Submit this transaction via RPC `send_transaction` or P2P relay. It passes `non_contextual_verify`, `pre_check`, and `verify_rtx`, and is committed to a block.
5. The resulting cell is on-chain with an unsupported type script hash type; any attempt to spend it will fail at script execution, permanently locking the capacity. [5](#0-4) [6](#0-5)

### Citations

**File:** verification/src/transaction_verifier.rs (L80-103)
```rust
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
}
```

**File:** verification/src/transaction_verifier.rs (L787-815)
```rust
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
}
```

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
    }
```

**File:** tx-pool/src/process.rs (L705-732)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```
