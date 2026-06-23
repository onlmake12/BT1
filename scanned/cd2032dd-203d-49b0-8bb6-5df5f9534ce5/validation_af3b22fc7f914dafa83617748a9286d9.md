### Title
`ScriptHashTypeVerifier` Does Not Validate Type Script `hash_type` — Only Lock Script Is Checked - (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates that the **lock script's** `hash_type` is within the enabled set (`{Data=0, Type=1, Data1=2, Data2=4}`). It never inspects the **type script's** `hash_type`. An attacker can craft a transaction whose lock script is fully valid but whose type script carries an unactivated `hash_type` (e.g., `Data3=6`, `Data4=8`, …). The transaction passes the non-contextual gate and is forwarded to the more expensive contextual/script-execution pipeline, bypassing the intended early-rejection fence.

---

### Finding Description

`ScriptHashTypeVerifier::verify()` is the sole non-contextual guard that enforces the `ENABLED_SCRIPT_HASH_TYPE` allowlist:

```
ENABLED_SCRIPT_HASH_TYPE = { 0 (Data), 1 (Type), 2 (Data1), 4 (Data2) }
```

The implementation reads:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
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
    }
    Ok(())
}
```

Only `output.lock().hash_type()` is examined. `output.type_()` is never touched.

The lower-level `check_data` function does visit both scripts, but it only calls `ScriptHashType::verify_value(v)`, which accepts any even value or `1` — i.e., it accepts `Data3=6`, `Data4=8`, …, `Data127=254`. Those values are structurally well-formed but are **not** in `ENABLED_SCRIPT_HASH_TYPE`. `check_data` therefore does **not** close the gap left by `ScriptHashTypeVerifier`.

The docstring of `NonContextualTransactionVerifier` itself confirms the omission: it lists "Check whether output **lock** hash type within enabled range" — the type script is not mentioned. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

A transaction with a valid lock script and a type script carrying `hash_type = 6` (`Data3`) passes:

1. `check_data` — `verify_value(6)` is `true` (6 is even).
2. `ScriptHashTypeVerifier::verify()` — only the lock script is checked; the type script is ignored.

The transaction therefore clears the entire non-contextual gate (`NonContextualTransactionVerifier`) and is forwarded to contextual verification and script execution. At script execution, `select_version()` will eventually reject it with `InvalidScriptHashType` or `InvalidVmVersion`, but only after the node has:

- Resolved all cell dependencies (database I/O).
- Allocated a script execution environment.
- Potentially admitted the transaction into the tx-pool and relayed it to peers.

Because the non-contextual check is the cheap, stateless gate that is supposed to drop malformed transactions before any of the above work is done, bypassing it constitutes a **resource-exhaustion / DoS** vector reachable by any unprivileged transaction sender or P2P relay peer. A flood of such transactions forces every receiving node to perform expensive contextual work for each one.

Additionally, the error variants `InvalidScriptHashType` and `ScriptHashTypeNotPermitted` are both marked `is_malformed_tx() = true`, meaning they are supposed to trigger early banning of the submitting peer. Because the type script path never reaches `ScriptHashTypeVerifier`, that banning logic is never triggered for type-script violations. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The entry path requires only the ability to submit a transaction via RPC (`send_transaction`) or via P2P relay. No keys, no stake, no special role. The crafted transaction is trivially constructed: take any valid transaction and set the type script's `hash_type` byte to `6`. The structural `check_data` pass guarantees the message is accepted at the network layer, and `ScriptHashTypeVerifier` will not reject it. This is straightforward to automate and repeat at high frequency. [7](#0-6) 

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the type script's `hash_type` when a type script is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type
        check_hash_type(output.lock().hash_type())?;

        // Check type script hash type if present
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}

fn check_hash_type(hash_type: packed::Byte) -> Result<(), Error> {
    if let Ok(ht) = TryInto::<ScriptHashType>::try_into(hash_type) {
        let val: u8 = ht.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err((TransactionError::InvalidScriptHashType { hash_type }).into());
    }
    Ok(())
}
```

Also update the `NonContextualTransactionVerifier` docstring to reflect that both lock and type script hash types are checked. [8](#0-7) 

---

### Proof of Concept

1. Construct a transaction with one output:
   - Lock script: `hash_type = 0` (Data) — valid, passes `ScriptHashTypeVerifier`.
   - Type script: `hash_type = 6` (Data3) — unactivated, but `verify_value(6) = true` so `check_data` passes.

2. Submit via `send_transaction` RPC or relay via P2P.

3. Observe: `NonContextualTransactionVerifier::verify()` returns `Ok(())`. The transaction proceeds to contextual verification (cell resolution, script execution setup).

4. Only at `select_version()` inside the script execution engine does the node finally reject it with `InvalidScriptHashType` — after all the expensive work has been done.

5. Repeat at high frequency to exhaust node resources. No banning occurs because `ScriptHashTypeVerifier` never fires for type-script violations. [9](#0-8) [4](#0-3) [1](#0-0)

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

**File:** util/gen-types/src/extension/check_data.rs (L10-27)
```rust
impl<'r> packed::ScriptReader<'r> {
    fn check_data(&self) -> bool {
        core::ScriptHashType::verify_value(self.hash_type().into())
    }
}

impl<'r> packed::ScriptOptReader<'r> {
    fn check_data(&self) -> bool {
        self.to_opt()
            .map(|i| core::ScriptHashType::verify_value(i.hash_type().into()))
            .unwrap_or(true)
    }
}

impl<'r> packed::CellOutputReader<'r> {
    fn check_data(&self) -> bool {
        self.lock().check_data() && self.type_().check_data()
    }
```

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
```

**File:** util/types/src/core/error.rs (L242-264)
```rust
impl TransactionError {
    /// Returns whether this transaction error indicates that the transaction is malformed.
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            TransactionError::OutputsSumOverflow { .. }
            | TransactionError::DuplicateCellDeps { .. }
            | TransactionError::DuplicateHeaderDeps { .. }
            | TransactionError::Empty { .. }
            | TransactionError::InsufficientCellCapacity { .. }
            | TransactionError::InvalidSince { .. }
            | TransactionError::ExceededMaximumBlockBytes { .. }
            | TransactionError::InvalidScriptHashType { .. }
            | TransactionError::ScriptHashTypeNotPermitted { .. }
            | TransactionError::OutputsDataLengthMismatch { .. } => true,

            TransactionError::Immature { .. }
            | TransactionError::CellbaseImmaturity { .. }
            | TransactionError::MismatchedVersion { .. }
            | TransactionError::Compatible { .. }
            | TransactionError::DaoLockSizeMismatch { .. }
            | TransactionError::Internal { .. } => false,
        }
    }
```

**File:** script/src/types.rs (L930-936)
```rust
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
```
