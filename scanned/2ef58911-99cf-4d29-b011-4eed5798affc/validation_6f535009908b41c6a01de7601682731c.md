### Title
Incomplete Script Hash-Type Validation Allows Disallowed Hash Types in Output Type Scripts — (`verification/src/transaction_verifier.rs`)

### Summary

`ScriptHashTypeVerifier::verify()` enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist only against the **lock script** of each transaction output. The **type script** of the same output is never checked. An unprivileged transaction sender can therefore submit a transaction whose output carries a type script with a hash type that is not yet permitted by the current consensus rules (e.g., `Data2` before the CKB v2023 hardfork), bypassing the only non-contextual gate that is supposed to prevent such scripts from entering the chain.

### Finding Description

`ScriptHashTypeVerifier` is part of `NonContextualTransactionVerifier` and is the designated early-rejection point for outputs whose scripts reference hash types outside the currently enabled set. [1](#0-0) 

The implementation iterates over every output and calls `output.lock().hash_type()`, validates it against `ENABLED_SCRIPT_HASH_TYPE`, and returns an error if the value is not permitted. However, it never calls `output.type_().to_opt()` and never validates the type script's hash type:

```rust
// verification/src/transaction_verifier.rs  ScriptHashTypeVerifier::verify()
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(...ScriptHashTypeNotPermitted...);
        }
    } else {
        return Err(...InvalidScriptHashType...);
    }
    // ← output.type_() is never inspected here
}
``` [1](#0-0) 

`ScriptHashTypeVerifier` is wired into `NonContextualTransactionVerifier`, which is the only place this class of check is performed before the transaction enters the pool or is committed to a block: [2](#0-1) 

The pattern is structurally identical to the reported analog: a validation check is applied to one code path (lock script) but is absent from a parallel code path (type script) that leads to the same privileged on-chain state.

### Impact Explanation

A transaction output's type script governs the lifecycle of the cell — it is executed whenever the cell is consumed as an input. Committing an output whose type script carries a hash type that is not yet enabled (e.g., `Data2` / `ScriptHashType::Data2` before the CKB v2023 hardfork epoch) has two concrete consequences:

1. **Consensus split**: Nodes that have crossed the hardfork boundary will execute the type script normally; nodes that have not will reject it. This creates a chain-split condition triggered by a single crafted transaction.
2. **Deferred DoS / unexpected execution**: If the script verifier encounters an unrecognised hash type at execution time (when the cell is later spent), the behaviour is implementation-defined. A panic or an unhandled error in the script scheduler can crash or stall the verifying node.

Both outcomes are reachable without any privileged key or operator access.

### Likelihood Explanation

Any RPC caller or P2P transaction relayer can craft such a transaction. The only precondition is knowledge of the gap (which is visible in the source). The attack is deterministic and requires no brute force, social engineering, or majority hash power. Likelihood is moderate: the window is bounded by the hardfork activation schedule, but the gap exists in every pre-activation epoch.

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output that carries one:

```rust
for output in self.transaction.outputs() {
    // existing lock-script check
    check_hash_type(output.lock().hash_type())?;

    // missing type-script check — add this
    if let Some(type_script) = output.type_().to_opt() {
        check_hash_type(type_script.hash_type())?;
    }
}
```

This mirrors the fix applied in the referenced report: the same invariant that is enforced in one path must be enforced in every path that can produce the same privileged state.

### Proof of Concept

1. Before the CKB v2023 hardfork epoch is active, construct a transaction with:
   - A lock script using `ScriptHashType::Data` (passes the existing check).
   - A type script using `ScriptHashType::Data2` (not in `ENABLED_SCRIPT_HASH_TYPE` pre-v2023, but never checked).
2. Submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier` calls `ScriptHashTypeVerifier::verify()`, which inspects only the lock script and returns `Ok(())`.
4. The transaction enters the pool and can be included in a block.
5. When the cell is later spent, the type script with `Data2` is executed. Pre-hardfork nodes reject it; post-hardfork nodes accept it — producing a consensus divergence. [3](#0-2) [4](#0-3)

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
