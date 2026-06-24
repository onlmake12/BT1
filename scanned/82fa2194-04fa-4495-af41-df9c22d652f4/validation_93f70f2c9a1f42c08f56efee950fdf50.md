Audit Report

## Title
`ScriptHashTypeVerifier` Skips Type Script Hash Type Enforcement, Allowing Consensus-Forbidden Hash Types Into Tx Pool — (`File: verification/src/transaction_verifier.rs`)

## Summary
`ScriptHashTypeVerifier::verify()` iterates over transaction outputs and enforces the consensus-enabled `hash_type` range only for the lock script, never for the optional type script. An unprivileged sender can submit a transaction whose type script carries a hash type outside `ENABLED_SCRIPT_HASH_TYPE` (e.g., `Data3 = 6`), and `NonContextualTransactionVerifier` will admit it to the tx pool without error. The `check_data()` path does visit both scripts but only validates structural discriminant validity, not the consensus-enabled subset, so it does not compensate for the missing check.

## Finding Description
`ScriptHashTypeVerifier::verify()` at lines 796–814 of `verification/src/transaction_verifier.rs` reads only `output.lock().hash_type()`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(...)
            }
        } else { ... }
    }
    Ok(())
}
```

`output.type_()` is never read. [1](#0-0) 

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}` — only `Data`, `Type`, `Data1`, `Data2`. [2](#0-1) 

The `ScriptHashType` enum defines future variants `Data3 = 6`, `Data4 = 8`, … `Data127 = 254` via a `seq!` macro expansion. These are valid molecule bytes and valid enum discriminants, but are not in `ENABLED_SCRIPT_HASH_TYPE`. [3](#0-2) 

`NonContextualTransactionVerifier::verify()` calls `self.script_hash_type.verify()` as a mandatory gate before tx-pool admission. [4](#0-3) 

`check_data()` on `CellOutputReader` does visit both lock and type scripts, but it only calls `ScriptHashType::verify_value()`, which accepts any byte that is even or equals 1 — it does not enforce the consensus-enabled subset. [5](#0-4) 

`verify_value()` itself confirms this: it passes any `v` where `v % 2 == 0 || v == 1`, which includes `6`, `8`, `10`, … [6](#0-5) 

## Impact Explanation
At minimum, this allows consensus-forbidden type script hash types to enter the tx pool, consuming relay bandwidth and pool slots (High: network congestion with few costs). If the block verifier carries the same gap — which is plausible given the block verifier also references `ENABLED_SCRIPT_HASH_TYPE` in the same pattern — a miner can include such a transaction in a block. Nodes that apply a stricter check would reject the block while others accept it, producing a consensus split reachable by any unprivileged sender (Critical: consensus deviation). The block verifier's behavior determines which tier applies, but the tx-pool admission gap is confirmed.

## Likelihood Explanation
Zero privilege is required. The attacker constructs a transaction with a valid lock script (`hash_type = 1`, enabled) and a type script with `hash_type = 6` (`Data3`, valid discriminant, not in `ENABLED_SCRIPT_HASH_TYPE`). This is a single-byte field in the serialized output. No key material, mining power, or social engineering is needed. The `ScriptHashType` enum variants are already defined in the codebase, making the target byte values trivially discoverable.

## Recommendation
Extend `ScriptHashTypeVerifier::verify()` to also validate the type script hash type for every output. After the existing lock-script check, add:

```rust
if let Some(type_script) = output.type_().to_opt() {
    let type_ht = type_script.hash_type();
    if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(type_ht) {
        let val: u8 = hash_type.into();
        if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
            return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
        }
    } else {
        return Err(TransactionError::InvalidScriptHashType { hash_type: type_ht }.into());
    }
}
```

Also audit the block verifier's `ENABLED_SCRIPT_HASH_TYPE` usage to confirm it applies the same check to type scripts.

## Proof of Concept
1. Construct a `CellOutput` with:
   - Lock script: `hash_type = 1` (`Type`, enabled)
   - Type script: `hash_type = 6` (`Data3`, valid discriminant, not in `ENABLED_SCRIPT_HASH_TYPE`)
2. Wrap it in a transaction and submit via `send_transaction` RPC.
3. `NonContextualTransactionVerifier::verify()` calls `ScriptHashTypeVerifier::verify()`.
4. The verifier checks only the lock script hash type (`1` → enabled → passes) and returns `Ok(())` without ever reading the type script hash type.
5. The transaction is accepted into the tx pool despite carrying a consensus-forbidden type script hash type.
6. A unit test can confirm this by constructing such a transaction and asserting `NonContextualTransactionVerifier::new(&tx, &consensus).verify()` returns `Ok(())`.

### Citations

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
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

**File:** util/gen-types/src/core.rs (L9-33)
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
```

**File:** util/gen-types/src/core.rs (L39-41)
```rust
    pub fn verify_value(v: u8) -> bool {
        v.is_multiple_of(2) || v == 1
    }
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
