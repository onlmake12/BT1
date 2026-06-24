The code confirms the claim. All cited references are accurate:

- `ScriptHashTypeVerifier::verify()` at lines 796–815 only calls `output.lock().hash_type()`, never `output.type_()`.
- `ENABLED_SCRIPT_HASH_TYPE` contains only `{0, 1, 2, 4}`.
- `ScriptHashType` enum is macro-generated for `N in 3..=127`, making `Data3 = 6`, `Data4 = 8`, etc. valid Rust variants not in the allowlist.
- `check_data` only checks structural validity (`verify_value`: even or 1), not the consensus allowlist.

Audit Report

## Title
`ScriptHashTypeVerifier::verify()` Skips `ENABLED_SCRIPT_HASH_TYPE` Check on Type Scripts — (File: `verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and validates the lock script `hash_type` against the consensus-gated `ENABLED_SCRIPT_HASH_TYPE` set, but never inspects the type script `hash_type` of the same outputs. Any unprivileged transaction sender can craft an output whose type script carries a `hash_type` value outside the currently permitted consensus range, and `NonContextualTransactionVerifier` will accept it without complaint, allowing the transaction to enter the tx-pool.

## Finding Description
In `verification/src/transaction_verifier.rs` lines 796–815, `ScriptHashTypeVerifier::verify()` loops over outputs and exclusively calls `output.lock().hash_type()`:

```rust
for output in self.transaction.outputs() {
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) { ... }
    } ...
}
```

`output.type_()` is never interrogated. `ENABLED_SCRIPT_HASH_TYPE` (in `util/constant/src/consensus.rs`) contains only `{0, 1, 2, 4}` (Data, Type, Data1, Data2). The `ScriptHashType` enum is macro-generated for all `N in 3..=127`, so `Data3 = 6`, `Data4 = 8`, etc. are valid Rust variants already. `check_data` in `util/gen-types/src/extension/check_data.rs` checks both lock and type scripts, but only via `ScriptHashType::verify_value()` (even-or-1 structural check), which passes `6` since `6 % 2 == 0` — it does not enforce the `ENABLED_SCRIPT_HASH_TYPE` allowlist. `ScriptHashTypeVerifier` is the sole early-rejection gate for hash type enforcement inside `NonContextualTransactionVerifier` (lines 71–102).

## Impact Explanation
**Tx-pool pollution / network congestion (High):** A transaction whose lock script uses `hash_type = 0` (permitted) and whose type script uses `hash_type = 6` (`Data3`, valid enum variant, not in `ENABLED_SCRIPT_HASH_TYPE`) passes `ScriptHashTypeVerifier` and enters the tx-pool. The transaction is only rejected later during contextual script execution. An attacker can flood the tx-pool with such transactions at negligible cost, wasting tx-pool slots and node CPU across the network. This matches the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

**Consensus divergence (Critical, conditional):** During a hard-fork transition window where a new CKB-VM release supports `DataN` before the corresponding epoch activates `ENABLED_SCRIPT_HASH_TYPE`, an attacker can submit a transaction whose type script uses `DataN`. Because `ScriptHashTypeVerifier` never checks the type script hash type, the transaction passes non-contextual verification, the type script executes successfully on upgraded nodes, and the transaction is committed to a block. Nodes on the old VM reject the block; nodes on the new VM accept it — a consensus-divergence / chain-split vector reachable by any unprivileged sender.

## Likelihood Explanation
The entry path is trivially reachable: any RPC caller (`send_transaction`) or P2P relay peer can submit a crafted transaction with no special privilege. Scenario A (tx-pool pollution) is exploitable today with any `ScriptHashType` variant not in the current `ENABLED_SCRIPT_HASH_TYPE` set (e.g., `6`, `8`, `10`, …). Scenario B requires a VM/consensus version skew, which is a realistic condition during any hard-fork transition window.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type when the type script is present:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        check_hash_type(output.lock().hash_type())?;
        if let Some(type_script) = output.type_().to_opt() {
            check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}
```

where `check_hash_type` encapsulates the existing `TryInto<ScriptHashType>` + `ENABLED_SCRIPT_HASH_TYPE.contains` logic.

## Proof of Concept
1. Craft a transaction with one output whose lock script uses `hash_type = 0` (Data, permitted) and whose type script uses `hash_type = 6` (`Data3`, valid enum variant, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC or P2P relay.
3. Observe that `NonContextualTransactionVerifier::verify()` → `ScriptHashTypeVerifier::verify()` returns `Ok(())` — the type script hash type is never inspected.
4. The transaction enters the tx-pool despite violating the `ENABLED_SCRIPT_HASH_TYPE` consensus rule for its type script.
5. Repeat at scale to exhaust tx-pool capacity. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** verification/src/transaction_verifier.rs (L796-815)
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

**File:** util/gen-types/src/core.rs (L9-42)
```rust
seq!(N in 3..=127 {
    /// Specifies how the script `code_hash` is used to match the script code and how to run the code.
    /// The hash type is split into the high 7 bits and the low 1 bit,
    /// when the low 1 bit is 1, it indicates the type,
    /// when the low 1 bit is 0, it indicates the data,
    /// and then it relies on the high 7 bits to indicate
    /// that the data actually corresponds to the version.
     #[derive(Default, Clone, Copy, PartialEq, Eq, Debug, Hash, FromRepr)]
     #[repr(u8)]
    pub enum ScriptHashType {
        /// Type "type" matches script code via cell type script hash.
        Type = 1,
        /// Type "data" matches script code via cell data hash, and run the script code in v0 CKB VM.
        #[default]
        Data = 0,
        /// Type "data1" matches script code via cell data hash, and run the script code in v1 CKB VM.
        Data1 = 2,
        /// Type "data2" matches script code via cell data hash, and run the script code in v2 CKB VM.
        Data2 = 4,
        #(
            #[doc = concat!("Type \"data", stringify!(N), "\" matches script code via cell data hash, and runs the script code in v", stringify!(N), " CKB VM.")]
            Data~N = N << 1,
        )*
    }
});

impl ScriptHashType {
    /// when the low 1 bit is 1, it indicates the type
    /// when the low 1 bit is 0, it indicates the data
    #[inline]
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
}
```
