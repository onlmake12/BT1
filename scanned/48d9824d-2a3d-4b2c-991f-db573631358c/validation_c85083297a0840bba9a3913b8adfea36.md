Audit Report

## Title
`ScriptHashTypeVerifier` Omits Type Script `hash_type` Validation, Enabling Tx-Pool Admission Bypass — (`File: verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces the consensus-permitted `hash_type` set only for the lock script, never inspecting the optional type script. A transaction output whose type script carries a `hash_type` value outside `ENABLED_SCRIPT_HASH_TYPE` (e.g., an invalid discriminant such as `3`) passes `NonContextualTransactionVerifier` and is admitted to the tx pool, bypassing the cheap non-contextual gate that is supposed to reject consensus-invalid transactions before contextual verification begins.

## Finding Description
`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` reads only `output.lock().hash_type()`:

```rust
// verification/src/transaction_verifier.rs L796-L814
if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
    let val: u8 = hash_type.into();
    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
} else { ... }
// output.type_() is never read
``` [1](#0-0) 

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` — value `3` is absent: [2](#0-1) 

The `ScriptHashType` enum currently defines discriminants `{0, 1, 2, 4}` (Data, Type, Data1, Data2). Value `3` is not a valid discriminant. For the **lock** script, `TryInto` on value `3` returns `Err`, triggering the `else` branch and returning `InvalidScriptHashType`. For the **type** script, no equivalent path exists — the field is never examined.

The structural `check_data` path in `util/gen-types/src/extension/check_data.rs` checks both scripts symmetrically:

```rust
fn check_data(&self) -> bool {
    self.lock().check_data() && self.type_().check_data()
}
``` [3](#0-2) 

However, `check_data` only validates that the byte is a valid enum discriminant via `ScriptHashType::verify_value` — it does **not** enforce `ENABLED_SCRIPT_HASH_TYPE`. On the **P2P relay path**, `check_data` would reject `hash_type = 3` (invalid discriminant). On the **RPC path** (`send_transaction`), `check_data` is not invoked before `NonContextualTransactionVerifier`, so a type script with `hash_type = 3` passes `ScriptHashTypeVerifier` and enters the tx pool.

`ScriptHashTypeVerifier` is a member of `NonContextualTransactionVerifier` and is the sole consensus-level gate for `hash_type` enforcement: [4](#0-3) 

The struct docstring at line 70 says "Check whether output lock hash type within enabled range" — confirming the type script omission is unintentional. [5](#0-4) 

## Impact Explanation
A transaction with a type script carrying `hash_type = 3` submitted via `send_transaction` RPC bypasses the non-contextual gate and enters the pending tx pool. The node then proceeds to contextual verification (script resolution, potential CKB-VM invocation) before evicting the transaction. Because the UTXO is not consumed on rejection, the attacker can resubmit variants repeatedly. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the attacker's cost is one or more live cells plus RPC access; the node bears contextual verification overhead per submission.

## Likelihood Explanation
The RPC `send_transaction` endpoint is fully unprivileged. Any caller with one or more live cells can craft a transaction with a valid lock script (`hash_type = 0`) and an invalid type script (`hash_type = 3`). No special role, key, or hash power is required. The attack is repeatable: since the UTXO is not spent on rejection, the same cell can be reused across submissions (with transaction variation to avoid duplicate rejection). The P2P relay path is blocked by `check_data`, but the RPC path is not.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when the type script is present, mirroring the symmetric pattern already used in `check_data`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // existing lock-script check (unchanged)
        match TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            Ok(ht) => {
                let val: u8 = ht.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                }
            }
            Err(_) => return Err(TransactionError::InvalidScriptHashType {
                hash_type: output.lock().hash_type(),
            }.into()),
        }
        // NEW: type-script check
        if let Some(type_script) = output.type_().to_opt() {
            match TryInto::<ScriptHashType>::try_into(type_script.hash_type()) {
                Ok(ht) => {
                    let val: u8 = ht.into();
                    if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                        return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
                    }
                }
                Err(_) => return Err(TransactionError::InvalidScriptHashType {
                    hash_type: type_script.hash_type(),
                }.into()),
            }
        }
    }
    Ok(())
}
```

## Proof of Concept
1. Obtain a live cell (any UTXO owned by the attacker).
2. Construct a `CellOutput` with:
   - `lock` script: `hash_type = 0` (Data, valid)
   - `type_` script: `hash_type = 3` (not in `ENABLED_SCRIPT_HASH_TYPE`, not a valid discriminant)
3. Wrap it in a transaction referencing the live cell as input.
4. Submit via `send_transaction` RPC.
5. Observe: `NonContextualTransactionVerifier::verify()` returns `Ok(())` — `ScriptHashTypeVerifier` passes because `output.type_()` is never read.
6. Transaction enters the pending pool; node proceeds to contextual verification before eviction.
7. Resubmit with a modified output (e.g., different capacity value) to avoid duplicate rejection; repeat to sustain pool pressure.

### Citations

**File:** verification/src/transaction_verifier.rs (L70-70)
```rust
/// - Check whether output lock hash type within enabled range
```

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

**File:** util/gen-types/src/extension/check_data.rs (L24-28)
```rust
impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
}
```
