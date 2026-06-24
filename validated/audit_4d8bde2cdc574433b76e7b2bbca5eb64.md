Audit Report

## Title
Incomplete `ScriptHashType` Validation in `ScriptHashTypeVerifier` — Type Script `hash_type` Not Checked — (`verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs but validates only the lock script's `hash_type`, never the type script's. Because `NonContextualTransactionVerifier` relies solely on this verifier as its hash-type gate, a transaction whose output carries a type script with an invalid (`hash_type = 3`) or not-yet-enabled (`hash_type = 6`) byte passes the cheap non-contextual check and proceeds to expensive contextual verification — a path reachable by any unprivileged RPC caller at negligible cost.

## Finding Description
`ScriptHashTypeVerifier::verify()` at [1](#0-0)  loops over outputs and calls `output.lock().hash_type()` only — `output.type_()` is never consulted. The struct's doc comment at [2](#0-1)  explicitly narrows the stated intent to "output **lock** hash type", confirming the omission is a design gap, not dead code.

`NonContextualTransactionVerifier::verify()` at [3](#0-2)  calls `self.script_hash_type.verify()` as its only hash-type gate and nothing else.

`ENABLED_SCRIPT_HASH_TYPE` at [4](#0-3)  contains `{0, 1, 2, 4}`. Values `3` and `6` are absent. For the lock script, value `3` causes `TryInto::<ScriptHashType>::try_into` to fail and return `InvalidScriptHashType`; value `6` (if a valid variant) is caught by the `ENABLED_SCRIPT_HASH_TYPE` membership check. Neither check is applied to the type script.

A more complete check exists in `CellOutputReader::check_data()` at [5](#0-4) , which checks both `self.lock().check_data() && self.type_().check_data()`. However, `TransactionReader::check_data()` is `pub(crate)` at [6](#0-5)  and is wired only into P2P relay handlers (`sync/src/relayer/mod.rs`, `sync/src/synchronizer/mod.rs`). It is never called from the RPC tx-pool admission path.

`tx-pool/src/util.rs` `non_contextual_verify` at [7](#0-6)  calls only `NonContextualTransactionVerifier::new(tx, consensus).verify()` — no `check_data()` invocation.

The invalid hash_type is only caught later inside `select_version()` at [8](#0-7)  during contextual script execution, after cell dep resolution and contextual verification setup have already consumed resources (see `verify_rtx` at [9](#0-8) ).

## Impact Explanation
Any RPC caller can repeatedly submit transactions with a type script `hash_type` of `3` or `6`. Each such transaction passes `NonContextualTransactionVerifier` (O(outputs), cheap), proceeds to `verify_rtx` → `ContextualTransactionVerifier`, triggering cell dep resolution and contextual verification setup, and is rejected only inside `select_version()`. At scale, this forces the node to perform expensive contextual verification work for transactions that should have been rejected at the non-contextual gate. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attack requires only a reachable RPC endpoint and the ability to set one byte in a molecule-encoded transaction. No cryptographic material, no on-chain funds, and no privileged access are needed. The crafted byte (`0x03` or `0x06`) in the type script `hash_type` field is trivial to produce. The attack is fully repeatable and automatable.

## Recommendation
**Short term:** Extend `ScriptHashTypeVerifier::verify()` to also iterate over `output.type_().to_opt()` and apply the same `ENABLED_SCRIPT_HASH_TYPE` check to the type script's `hash_type`, mirroring the completeness already present in `CellOutputReader::check_data()`.

**Long term:** Unify the structural (`verify_value`) and consensus-enabled (`ENABLED_SCRIPT_HASH_TYPE`) checks into a single validation step invoked consistently for every script field — lock, type, and cell-dep scripts — at the earliest possible point in both the RPC and P2P ingestion paths, eliminating the divergence between the two code paths.

## Proof of Concept
1. Construct a raw molecule-encoded transaction with one output whose type script has `hash_type = 0x03` (byte value 3).
2. Submit via `send_transaction` RPC.
3. Observe: `NonContextualTransactionVerifier::verify()` returns `Ok(())` — `ScriptHashTypeVerifier` never inspected the type script's `hash_type`.
4. The transaction proceeds to `ContextualTransactionVerifier`; cell dep resolution runs.
5. Inside `select_version()`, `ScriptHashType::try_from(3u8)` returns `Err`, and the transaction is rejected — but only after consuming contextual verification resources.
6. Repeat with `hash_type = 0x06` to confirm the same bypass for a structurally valid but not-yet-enabled value.
7. Automate at high frequency to sustain resource pressure on the node's tx-pool processing pipeline.

### Citations

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
```

**File:** verification/src/transaction_verifier.rs (L94-101)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
```

**File:** verification/src/transaction_verifier.rs (L796-814)
```rust
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

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** util/gen-types/src/extension/check_data.rs (L24-27)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
```

**File:** util/gen-types/src/extension/check_data.rs (L57-59)
```rust
    pub(crate) fn check_data(&self) -> bool {
        self.raw().check_data()
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

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** script/src/types.rs (L903-904)
```rust
        let script_hash_type = ScriptHashType::try_from(script.hash_type())
            .map_err(|err| ScriptError::InvalidScriptHashType(err.to_string()))?;
```
