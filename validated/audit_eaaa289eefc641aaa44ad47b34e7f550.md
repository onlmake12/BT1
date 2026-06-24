All code references check out exactly as claimed. The findings are confirmed:

- `ScriptHashTypeVerifier::verify()` at lines 796–814 only calls `output.lock().hash_type()` — `output.type_()` is never examined. [1](#0-0) 
- The doc comment at line 70 explicitly scopes the check to lock scripts only. [2](#0-1) 
- `ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` — `Data3 = 6` is absent. [3](#0-2) 
- The non-contextual gate passes, the tx is enqueued, and `select_version` in `script/src/types.rs` is the first place the invalid type script hash_type is caught. [4](#0-3) 
- The full contextual pipeline (`pre_check` DB resolution → `ContextualTransactionVerifier` → `TransactionScriptsVerifier`) runs before rejection. [5](#0-4) 
- A grep for `type_().*hash_type` in `verification/src/` returns no matches, confirming no other non-contextual check covers this gap. [6](#0-5) 

---

Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation on Transaction Outputs — (File: `verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's lock script against `ENABLED_SCRIPT_HASH_TYPE`, but never inspects the type script on the same output. An unprivileged caller can submit a transaction via `send_transaction` RPC with a type script carrying an unenabled `hash_type` (e.g., `Data3 = 6`). The transaction passes the cheap non-contextual gate, enters the verify queue, and forces a full contextual verification cycle — including DB I/O and script-group construction — before being rejected, enabling sustained resource exhaustion at near-zero cost.

## Finding Description

**Root cause — `ScriptHashTypeVerifier::verify()` only reads `output.lock()`:**

In `verification/src/transaction_verifier.rs` lines 796–814, the loop body calls `output.lock().hash_type()` exclusively. The `output.type_()` field is never examined:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // lock only
        { ... }
    }
    Ok(())   // output.type_() never checked
}
```

The doc comment at line 70 confirms this is the stated scope: `/// - Check whether output lock hash type within enabled range`. Type scripts are structurally absent.

`ENABLED_SCRIPT_HASH_TYPE` in `util/constant/src/consensus.rs` is `{0, 1, 2, 4}` (Data, Type, Data1, Data2). Any byte value not in this set (e.g., `6` for `Data3`) is a structurally valid but unenabled `ScriptHashType`.

**Exploit flow:**

1. Attacker calls `send_transaction` with `outputs_validator = "passthrough"` and a transaction whose output has a valid lock script (`hash_type = "type"`) and a type script with `hash_type = "data3"` (byte `6`).
2. `resumeble_process_tx()` in `tx-pool/src/process.rs` calls `non_contextual_verify()` → `ScriptHashTypeVerifier::verify()`. Only the lock script is checked; the check passes.
3. The transaction is enqueued in the verify queue via `enqueue_verify_queue`.
4. `_process_tx()` calls `pre_check()` (resolves input cells via DB lookups), then `verify_rtx()` → `ContextualTransactionVerifier::verify()` → `TransactionScriptsVerifier` → `select_version()`.
5. `select_version()` in `script/src/types.rs` hits the catch-all arm for `Data3` and returns `ScriptError::InvalidScriptHashType(...)`.
6. Transaction is rejected — but only after consuming DB I/O for input resolution, script-group construction, and the full contextual verification pipeline.

Since the transaction is rejected (never included in a block), the attacker's UTXO is never consumed. Varying the type script `args` field changes the tx hash, bypassing the `verify_queue_contains` deduplication check, enabling an unbounded stream of crafted transactions.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The non-contextual verifier is the designated cheap gate before the expensive contextual pipeline. By missing the type script check, every crafted transaction forces: (a) DB lookups to resolve input cells in `pre_check`, (b) `ResolvedTransaction` construction, (c) `CapacityVerifier` and `TimeRelativeTransactionVerifier` execution, and (d) `TransactionScriptsVerifier` initialization and `select_version` dispatch — all before the error is surfaced. At scale, this saturates the tx-pool admission worker and the verify queue, degrading throughput for legitimate transactions.

## Likelihood Explanation

The entry point is the public `send_transaction` JSON-RPC method, reachable by any unprivileged peer or local caller. Required attacker resources: one live UTXO (to satisfy input resolution), minimum fee rate (to pass the fee check), and knowledge that `hash_type = "data3"` is accepted by the JSON schema but not by the allowlist. No keys beyond the UTXO owner key, no operator access, and no majority hashpower are required. The attack is trivially repeatable by varying the type script `args` field to bypass the `verify_queue_contains` deduplication check, since rejected transactions do not consume the input UTXO.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the `hash_type` of type scripts when present on outputs:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err((TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }).into());
        }

        // add: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                }).into());
            }
        }
    }
    Ok(())
}
```

Also update the `NonContextualTransactionVerifier` doc comment at line 70 to reflect that both lock and type script hash types are checked.

## Proof of Concept

Submit via `send_transaction` RPC:

```json
{
  "id": 1, "jsonrpc": "2.0", "method": "send_transaction",
  "params": [
    {
      "version": "0x0",
      "cell_deps": [{ "out_point": { "tx_hash": "<secp_dep_tx>", "index": "0x0" }, "dep_type": "dep_group" }],
      "header_deps": [],
      "inputs": [{ "previous_output": { "tx_hash": "<attacker_utxo_tx>", "index": "0x0" }, "since": "0x0" }],
      "outputs": [{
        "capacity": "0x...",
        "lock": { "code_hash": "<secp256k1_type_hash>", "hash_type": "type", "args": "<pubkey_hash>" },
        "type": { "code_hash": "0x0000000000000000000000000000000000000000000000000000000000000001",
                  "hash_type": "data3",
                  "args": "0x" }
      }],
      "outputs_data": ["0x"],
      "witnesses": ["<valid_witness>"]
    },
    "passthrough"
  ]
}
```

**Without fix:** `ScriptHashTypeVerifier::verify()` returns `Ok(())` (type script `hash_type = data3` is not inspected). The transaction proceeds through `pre_check` (DB resolution), `CapacityVerifier`, `TimeRelativeTransactionVerifier`, and into `TransactionScriptsVerifier::select_version`, which returns `ScriptError::InvalidScriptHashType`. Rejection occurs only after all contextual-verification resources are consumed. Repeating with varied `args` bypasses deduplication, enabling sustained resource exhaustion at near-zero cost.

**With fix:** `ScriptHashTypeVerifier::verify()` returns `TransactionError::ScriptHashTypeNotPermitted { hash_type: 6 }` immediately during non-contextual verification, before any DB I/O or script-execution work is performed.

### Citations

**File:** verification/src/transaction_verifier.rs (L61-70)
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
```

**File:** verification/src/transaction_verifier.rs (L785-815)
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

**File:** script/src/types.rs (L930-935)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
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
