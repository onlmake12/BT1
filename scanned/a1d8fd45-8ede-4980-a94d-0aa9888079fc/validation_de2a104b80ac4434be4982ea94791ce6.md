### Title
`ScriptHashTypeVerifier` Skips `ENABLED_SCRIPT_HASH_TYPE` Check for Type Scripts, Allowing Invalid Transactions into the Tx-Pool — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` consensus restriction only on the **lock script** of each output, silently skipping the same check for the **type script**. A transaction whose output carries a structurally-valid but consensus-disabled hash type (e.g. `Data3 = 6`) on its type script passes non-contextual verification and is admitted to the tx-pool, yet can never be committed to a valid block. This is a direct analog to the Seaport finding: a parameter check that is applied to one variant (lock script) is silently omitted for another variant (type script), creating a divergence between what the tx-pool accepts and what consensus actually permits.

---

### Finding Description

`NonContextualTransactionVerifier` is the first gate for every transaction entering the tx-pool. It includes `ScriptHashTypeVerifier`: [1](#0-0) 

The loop iterates over every output and calls `output.lock().hash_type()` — the **lock** script only. The type script (`output.type_()`) is never inspected here. A transaction whose output has:

- `lock.hash_type` ∈ `ENABLED_SCRIPT_HASH_TYPE` (e.g. `Type = 1`) — passes the check
- `type_.hash_type` = `Data3 = 6` (structurally valid per `ScriptHashType::verify_value`, not in `ENABLED_SCRIPT_HASH_TYPE`) — **never checked**

The low-level `check_data()` call (used in the relay/sync layer) only validates structural bit-pattern validity, not the consensus-level `ENABLED_SCRIPT_HASH_TYPE` allowlist: [2](#0-1) 

So `Data3 = 6` passes `check_data()` (6 is even → structurally valid) and also passes `ScriptHashTypeVerifier` (type script not checked). The transaction is admitted to the tx-pool.

When a miner later tries to include the transaction in a block, `ContextualTransactionVerifier` runs script execution. `select_version()` rejects the unknown VM version: [3](#0-2) 

The block is rejected. The tx-pool accepted a transaction that consensus will never commit.

The `ENABLED_SCRIPT_HASH_TYPE` constant governs which hash types are currently permitted: [4](#0-3) 

Block-level verification (`BlockTxsVerifier`) does **not** call `NonContextualTransactionVerifier`; it goes directly to `ContextualTransactionVerifier`: [5](#0-4) 

So the `ScriptHashTypeVerifier` gap is never compensated at the block-verification stage.

---

### Impact Explanation

An unprivileged RPC caller or tx-pool submitter can craft transactions with outputs whose type scripts carry a structurally-valid but consensus-disabled `hash_type` (any even value ≥ 6 not yet activated). These transactions:

1. Pass `check_data()` in the relay/sync layer.
2. Pass `ScriptHashTypeVerifier` in `NonContextualTransactionVerifier`.
3. Are admitted to the tx-pool and consume pool slots and memory.
4. Can never be committed to a valid block (script execution rejects them).

The result is a **tx-pool pollution / resource exhaustion** vector: an attacker can continuously submit such transactions to displace legitimate transactions from the pool, degrading throughput and potentially causing valid transactions to be evicted. This is a medium-severity DoS on the tx-pool, reachable by any RPC caller without any privileged access.

---

### Likelihood Explanation

The attack requires only the ability to call `send_transaction` (or `submit_local_test_tx`) via the public JSON-RPC interface. No keys, no hashpower, no Sybil capability is needed. The crafted transaction is trivially constructable: set any output's type script `hash_type` to `0x06` (`Data3`). The tx-pool has a finite capacity, so sustained submission of such transactions can crowd out legitimate ones.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` for each output, mirroring the existing lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash_type (existing)
        let lock_hash_type = output.lock().hash_type();
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(lock_hash_type) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
        } else {
            return Err(TransactionError::InvalidScriptHashType { hash_type: lock_hash_type }.into());
        }

        // Check type script hash_type (missing — add this)
        if let Some(type_script) = output.type_().to_opt() {
            let type_hash_type = type_script.hash_type();
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_hash_type) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            } else {
                return Err(TransactionError::InvalidScriptHashType { hash_type: type_hash_type }.into());
            }
        }
    }
    Ok(())
}
```

This ensures the tx-pool and consensus agree on which transactions are admissible, eliminating the divergence.

---

### Proof of Concept

1. Construct a transaction with one output whose `lock` uses `hash_type = Type (1)` (valid) and whose `type_` uses `hash_type = 0x06` (`Data3`, structurally valid, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. Observe: `NonContextualTransactionVerifier` passes; the transaction enters the tx-pool.
4. Attempt to mine a block containing this transaction.
5. Observe: block verification fails at script execution (`InvalidVmVersion` or `InvalidScriptHashType`); the block is rejected.
6. The tx-pool slot remains occupied by an uncommittable transaction.

Repeat at scale to exhaust tx-pool capacity and displace legitimate transactions.

### Citations

**File:** verification/src/transaction_verifier.rs (L5-5)
```rust
use ckb_constant::consensus::ENABLED_SCRIPT_HASH_TYPE;
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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```

**File:** script/src/types.rs (L900-936)
```rust
    pub fn select_version(&self, script: &Script) -> Result<ScriptVersion, ScriptError> {
        let is_vm_version_2_and_syscalls_3_enabled = self.is_vm_version_2_and_syscalls_3_enabled();
        let is_vm_version_1_and_syscalls_2_enabled = self.is_vm_version_1_and_syscalls_2_enabled();
        let script_hash_type = ScriptHashType::try_from(script.hash_type())
            .map_err(|err| ScriptError::InvalidScriptHashType(err.to_string()))?;
        match script_hash_type {
            ScriptHashType::Data => Ok(ScriptVersion::V0),
            ScriptHashType::Data1 => {
                if is_vm_version_1_and_syscalls_2_enabled {
                    Ok(ScriptVersion::V1)
                } else {
                    Err(ScriptError::InvalidVmVersion(1))
                }
            }
            ScriptHashType::Data2 => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else {
                    Err(ScriptError::InvalidVmVersion(2))
                }
            }
            ScriptHashType::Type => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else if is_vm_version_1_and_syscalls_2_enabled {
                    Ok(ScriptVersion::V1)
                } else {
                    Ok(ScriptVersion::V0)
                }
            }
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L425-456)
```rust
                } else {
                    ContextualTransactionVerifier::new(
                        Arc::clone(tx),
                        Arc::clone(&self.context.consensus),
                        self.context.store.as_data_loader(),
                        Arc::clone(&tx_env),
                    )
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
                    .map_err(|error| {
                        BlockTransactionsError {
                            index: index as u32,
                            error,
                        }
                        .into()
                    })
                    .map(|completed| (wtx_hash, completed))
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
            })
            .skip(1) // skip cellbase tx
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;
```
