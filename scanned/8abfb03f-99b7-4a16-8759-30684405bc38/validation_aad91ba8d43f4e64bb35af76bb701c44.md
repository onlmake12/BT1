### Title
Asymmetric `ScriptHashType` Enforcement: Type Scripts Bypass Non-Contextual Hash-Type Consensus Gate — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier` — the non-contextual consensus gate that rejects transactions using hash types not yet enabled by consensus — only validates the **lock script** of each output. It never inspects the **type script** of the same output. An attacker can craft a transaction whose type script carries a disallowed (e.g., pre-hardfork) `ScriptHashType`, bypass the non-contextual check entirely, and push the transaction into the tx-pool's contextual/script-execution stage. If the CKB-VM already implements the new hash-type semantics (which is the normal development pattern: VM ships first, consensus enables later), the transaction can execute successfully and reach a block before the hardfork activates, constituting a consensus-bypass.

---

### Finding Description

`NonContextualTransactionVerifier` includes `ScriptHashTypeVerifier` to enforce that every output's scripts use only hash types listed in `ENABLED_SCRIPT_HASH_TYPE`. The verifier iterates over all outputs but calls only `output.lock().hash_type()`: [1](#0-0) 

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

`output.type_()` is never consulted. A type script carrying a disallowed hash-type value passes this verifier unconditionally.

`NonContextualTransactionVerifier::verify()` is the only call-site for `ScriptHashTypeVerifier`: [2](#0-1) 

`ContextualTransactionVerifier::verify()` does **not** re-invoke `ScriptHashTypeVerifier`; it proceeds directly to `time_relative`, `capacity`, and `script` (VM execution): [3](#0-2) 

Both the local-RPC path (`process_tx` → `_process_tx`) and the remote-relay path (`submit_remote_tx` → `resumeble_process_tx`) call `non_contextual_verify` first, then proceed to contextual verification: [4](#0-3) [5](#0-4) 

The non-contextual check is the **only** consensus-level gate for disallowed hash types. Because it is incomplete for type scripts, the gate is bypassable.

---

### Impact Explanation

CKB's development pattern is: the VM implements new `ScriptHashType` semantics first; consensus enables them later via a hardfork epoch. `ENABLED_SCRIPT_HASH_TYPE` is the enforcement boundary between "implemented" and "consensus-active." By omitting the type-script check, any transaction author can:

1. Construct a transaction whose output type script uses a future/disallowed hash type.
2. Submit it via RPC (`send_transaction`) or P2P relay — both paths pass `ScriptHashTypeVerifier` without error.
3. The transaction reaches contextual verification (VM execution). If the VM already handles the new hash type, the type script executes successfully.
4. The transaction enters the pool and can be mined into a block **before the hardfork epoch activates**, violating consensus.

Even if the VM rejects the hash type at execution time, the transaction still consumes contextual-verification resources (cell resolution, capacity checks, partial script setup) that the non-contextual gate was designed to avoid.

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P transaction sender can trigger this. No special keys, no majority hashpower, no social engineering. The attacker only needs to know which `ScriptHashType` byte value is implemented in the VM but not yet in `ENABLED_SCRIPT_HASH_TYPE` — information that is publicly visible in the open-source codebase. Likelihood is **medium-high** whenever a new hash type is in the implementation pipeline.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also check the type script of each output, mirroring the lock-script check:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
        let lock_ht: u8 = TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
            .map_err(|_| TransactionError::InvalidScriptHashType { hash_type: output.lock().hash_type() })?
            .into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&lock_ht) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: lock_ht }.into());
        }

        // Check type script hash type (missing — add this)
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

Also apply the same check to cell-dep and input scripts if those paths are reachable before contextual verification.

---

### Proof of Concept

1. Identify a `ScriptHashType` byte value that is defined in the Rust enum and handled by the VM but absent from `ENABLED_SCRIPT_HASH_TYPE` (e.g., a value reserved for a future hardfork).
2. Construct a `TransactionView` whose output has:
   - A valid lock script with an **enabled** hash type (passes the existing check).
   - A type script with the **disallowed** hash type.
3. Submit via `send_transaction` RPC or P2P relay.
4. Observe: `NonContextualTransactionVerifier::verify()` returns `Ok(())` — the transaction is not rejected at the gate.
5. The transaction proceeds to `ContextualTransactionVerifier::verify()` → `script.verify()`.
6. If the VM resolves the type script successfully (because the hash type is implemented), the transaction is accepted into the pool and eligible for block inclusion, bypassing the hardfork activation epoch. [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L71-102)
```rust
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

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L401-426)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
        }
    }
```
