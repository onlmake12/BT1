Audit Report

## Title
Type Script `hash_type` Not Validated in `ScriptHashTypeVerifier::verify()`, Allowing Bypass of Non-Contextual Consensus Gate — (`File: verification/src/transaction_verifier.rs`)

## Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the `hash_type` of each output's **lock script** against `ENABLED_SCRIPT_HASH_TYPE`, but never inspects the **type script**'s `hash_type`. Because any CKB output may carry an optional type script, a transaction whose type script carries a `hash_type` that is a valid `ScriptHashType` enum variant but excluded from `ENABLED_SCRIPT_HASH_TYPE` (e.g., `Data3`) passes the non-contextual gate on every node and proceeds to contextual script execution. During a hard-fork transition period — when upgraded nodes recognise the new hash type and non-upgraded nodes do not — this produces divergent acceptance decisions and a consensus split.

## Finding Description

`ScriptHashTypeVerifier` is the sole non-contextual gate for rejecting transactions that reference script hash types not yet permitted by current fork rules. Its `verify()` loop reads:

```rust
// verification/src/transaction_verifier.rs  L796-811
for output in self.transaction.outputs() {
    if let Ok(hash_type) =
        TryInto::<ScriptHashType>::try_into(output.lock().hash_type())  // lock only
    {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err((TransactionError::InvalidScriptHashType {
            hash_type: output.lock().hash_type(),
        }).into());
    }
}
```

`output.type_()` is never consulted. `ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` (Data, Type, Data1, Data2); `ScriptHashType::Data3` is a defined enum variant that is **not** in this set. A transaction output whose lock uses `hash_type = 0x01` (Type, permitted) and whose type script uses `hash_type = Data3` (excluded) passes `ScriptHashTypeVerifier::verify()` without error and is forwarded to contextual verification.

The existing unit tests (`test_unknown_hash_type_output_lock`, `test_not_enabled_hash_type_output_lock`) cover only the lock-script path; no test exercises the type-script path.

The comment on `NonContextualTransactionVerifier` itself acknowledges only the lock: *"Check whether output lock hash type within enabled range"* — confirming the type script was never considered.

## Impact Explanation

**Consensus deviation (Critical, 15001–25000 points).**

During a CKB hard-fork activation window (as occurred for v2021 and v2023), upgraded nodes add the new hash type to their script layer while non-upgraded nodes do not. Because the non-contextual gate does not check the type script's `hash_type`:

1. A crafted transaction with a valid lock `hash_type` and a type script carrying the newly-activated `hash_type` passes `ScriptHashTypeVerifier` on **all** nodes (upgraded and non-upgraded alike).
2. At contextual verification, upgraded nodes execute the type script successfully and accept the transaction; non-upgraded nodes encounter an unrecognised hash type and reject it.
3. The two populations reach different chain states — a consensus split.

Even absent a fork, the bug allows any user to force every node to run the full contextual script-verification pipeline (script group construction, VM setup, execution) for transactions that should have been rejected at the cheap non-contextual gate, enabling low-cost resource exhaustion.

## Likelihood Explanation

No special key material, miner cooperation, or chain state is required. Any unprivileged caller of the `send_transaction` RPC can submit a transaction with an arbitrary `hash_type` byte in a type script. The attack path is fully reachable on every node that processes externally submitted transactions. During any hard-fork transition — a recurring event in CKB's history — the consensus-split variant becomes immediately exploitable by any observer.

## Recommendation

Extend `ScriptHashTypeVerifier::verify()` to apply the identical `ENABLED_SCRIPT_HASH_TYPE` check to the type script when it is present:

```rust
for output in self.transaction.outputs() {
    // existing lock-script check (unchanged) …

    // add: type-script check
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
            }).into());
        }
    }
}
```

Add corresponding unit tests analogous to `test_unknown_hash_type_output_lock` and `test_not_enabled_hash_type_output_lock` but targeting the type-script field.

## Proof of Concept

1. Build a `TransactionView` with one output:
   - `lock` script: `hash_type = 0x01` (ScriptHashType::Type, in `ENABLED_SCRIPT_HASH_TYPE`)
   - `type_` script: `hash_type = ScriptHashType::Data3` (valid enum variant, **not** in `ENABLED_SCRIPT_HASH_TYPE`)
2. Instantiate `ScriptHashTypeVerifier::new(&tx)` and call `.verify()`.
3. Observe `Ok(())` — the verifier returns success despite the type script carrying a non-permitted hash type.
4. The same transaction submitted via `send_transaction` RPC enters the tx pool and proceeds to `ContextualTransactionVerifier`, consuming contextual verification resources and, during a fork transition, producing divergent outcomes across node versions.

This is directly reproducible as a unit test mirroring the existing `test_not_enabled_hash_type_output_lock` test at `verification/src/tests/transaction_verifier.rs` L100–122, substituting the type-script field for the lock-script field. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L70-78)
```rust
/// - Check whether output lock hash type within enabled range
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
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

**File:** verification/src/tests/transaction_verifier.rs (L82-122)
```rust
pub fn test_unknown_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(Script::default().as_builder().hash_type(3).build())
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::InvalidScriptHashType {
            hash_type: 3.into(),
        },
    );
}

#[test]
pub fn test_not_enabled_hash_type_output_lock() {
    let transaction = TransactionBuilder::default()
        .output(
            CellOutput::new_builder()
                .lock(
                    Script::default()
                        .as_builder()
                        .hash_type(ScriptHashType::Data3)
                        .build(),
                )
                .build(),
        )
        .build();
    let verifier = ScriptHashTypeVerifier::new(&transaction);

    assert_error_eq!(
        verifier.verify().unwrap_err(),
        TransactionError::ScriptHashTypeNotPermitted {
            hash_type: ScriptHashType::Data3.into(),
        },
    );
}
```
