### Title
`ScriptHashTypeVerifier` Only Enforces Hash-Type Restriction on Output Lock Scripts, Silently Skipping Output Type Scripts — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`ScriptHashTypeVerifier` is the consensus-level gate that prevents disallowed or not-yet-activated `ScriptHashType` values from entering the chain. Its `verify()` implementation iterates over `transaction.outputs()` and checks only `output.lock().hash_type()`. The `output.type_()` field — the type script — is never inspected. Any transaction author can therefore embed a future or consensus-disabled hash type (e.g., `Data3 = 6`) inside an output's type script and pass this verifier without error. If the running CKB-VM binary already supports that VM version (which is the normal state during a staged activation), the type script executes successfully and the transaction is accepted, bypassing the activation gate entirely.

---

### Finding Description

**Root cause — `ScriptHashTypeVerifier::verify()` in `verification/src/transaction_verifier.rs` lines 796–813:**

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        if let Ok(hash_type) =
            TryInto::<ScriptHashType>::try_into(output.lock().hash_type())
        {
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

The loop body calls `output.lock()` exclusively. `output.type_().to_opt()` is never touched. The `ScriptHashType` enum is defined with a `seq!` macro that pre-declares `Data3` through `Data127` (values 6, 8, … 254) as valid Rust variants:

```rust
seq!(N in 3..=127 {
    Data~N = N << 1,
}*);
```

`ENABLED_SCRIPT_HASH_TYPE` (defined in `util/constant/src/consensus.rs`) contains only the currently activated subset (e.g., `[0, 1, 2, 4]` for Data, Type, Data1, Data2). A lock script with `hash_type = 6` is caught and rejected. A type script with `hash_type = 6` is invisible to this verifier.

**Exploit path:**

1. Attacker crafts a transaction whose output carries a type script with `hash_type = Data3 (6)`.
2. `ScriptHashTypeVerifier::verify()` iterates outputs, reads only `output.lock().hash_type()` (e.g., `Data = 0`, which is allowed), and returns `Ok(())`.
3. `TransactionScriptsVerifier` runs the type script group. If the CKB-VM binary already supports VM version 3 (which is the normal situation during a phased hardfork rollout — the binary ships support before consensus activates it), `select_version` succeeds and the script executes.
4. The transaction is accepted into the chain on nodes running the newer binary, while nodes running an older binary that returns `InvalidVmVersion` reject it — producing a **consensus split**.

The structural parallel to the GuardCM finding is exact:

| GuardCM | CKB |
|---|---|
| `delegatecall` restriction only enforced when `to == owner` | Hash-type restriction only enforced for `output.lock()` |
| `delegatecall` to any other address is unrestricted | Type script hash type is completely unchecked |
| Attacker bypasses guard by targeting non-owner | Attacker bypasses activation gate by using type script |

---

### Impact Explanation

**Consensus split / premature feature activation.** CKB's staged hardfork model ships VM support in the binary before the consensus switch activates it. `ScriptHashTypeVerifier` is the enforcement point that keeps the two in sync. Because it ignores type scripts, a transaction author can activate a future VM version unilaterally for type scripts. Nodes running the new binary accept the transaction; nodes running the old binary reject it. This is a chain-splitting condition. Additionally, any security properties that depend on a hash type being disabled (e.g., a VM version with known issues that is intentionally held back) are bypassed for type scripts.

---

### Likelihood Explanation

The attacker is an ordinary transaction sender — no privileged access, no keys, no majority hashpower. The only precondition is that the CKB-VM binary already contains support for the target VM version (the normal state during any hardfork preparation window). The crafted transaction is submitted via the standard RPC (`send_transaction`). The gap is reachable on every transaction that includes an output with a type script.

---

### Recommendation

Extend `ScriptHashTypeVerifier::verify()` to also validate the hash type of each output's type script:

```rust
pub fn verify(&self) -> Result<(), Error> {
    for output in self.transaction.outputs() {
        // Check lock script hash type (existing)
        Self::check_hash_type(output.lock().hash_type())?;

        // Check type script hash type (missing — add this)
        if let Some(type_script) = output.type_().to_opt() {
            Self::check_hash_type(type_script.hash_type())?;
        }
    }
    Ok(())
}

fn check_hash_type(raw: packed::Byte) -> Result<(), Error> {
    match TryInto::<ScriptHashType>::try_into(raw) {
        Ok(hash_type) => {
            let val: u8 = hash_type.into();
            if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                return Err(TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into());
            }
            Ok(())
        }
        Err(_) => Err((TransactionError::InvalidScriptHashType { hash_type: raw }).into()),
    }
}
```

---

### Proof of Concept

1. Build a transaction with one output whose **lock** script uses `hash_type = Data (0)` (allowed) and whose **type** script uses `hash_type = 6` (`Data3`, not in `ENABLED_SCRIPT_HASH_TYPE`).
2. Submit via `send_transaction` RPC.
3. Observe that `ScriptHashTypeVerifier` returns `Ok(())` — the type script hash type is never inspected.
4. On a node whose CKB-VM binary supports VM version 3, the type script executes and the transaction is accepted.
5. On a node whose binary does not support VM version 3, `select_version` returns `InvalidVmVersion(3)` and the transaction is rejected.
6. The two nodes now disagree on chain state — consensus split confirmed. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** verification/src/transaction_verifier.rs (L787-815)
```rust
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

**File:** util/jsonrpc-types/src/blockchain.rs (L16-47)
```rust
seq!(N in 3..=127 {
    /// Specifies how the script `code_hash` is used to match the script code and how to run the code.
    ///
    /// Allowed kinds: "data", "type", "data1" and "data2"
    ///
    /// Refer to the section [Code Locating](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0022-transaction-structure/0022-transaction-structure.md#code-locating)
    /// and [Upgradable Script](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0022-transaction-structure/0022-transaction-structure.md#upgradable-script)
    /// in the RFC *CKB Transaction Structure*.
    ///
    /// The hash type is split into the high 7 bits and the low 1 bit,
    /// when the low 1 bit is 1, it indicates the type,
    /// when the low 1 bit is 0, it indicates the data,
    /// and then it relies on the high 7 bits to indicate
    /// that the data actually corresponds to the version.
    #[derive(Default, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
    #[serde(rename_all = "snake_case")]
    #[repr(u8)]
    pub enum ScriptHashType {
        /// Type "data" matches script code via cell data hash, and runs the script code in v0 CKB VM
        #[default]
        Data = 0,
        /// Type "type" matches script code via cell type script hash.
        Type = 1,
        /// Type "data1" matches script code via cell data hash, and runs the script code in v1 CKB VM
        Data1 = 2,
        /// Type "data2" matches script code via cell data hash, and runs the script code in v2 CKB VM
        Data2 = 4,
        #(
            #[doc = concat!("Type \"data", stringify!(N), "\" matches script code via cell data hash, and runs the script code in v", stringify!(N), " CKB VM.")]
            Data~N = N << 1,
        )*
    }
```

**File:** util/types/src/core/blockchain.rs (L1-14)
```rust
use ckb_error::OtherError;

use crate::packed;

/// The DepType enum represents different types of dependencies for `cell_deps`.
#[derive(Default, Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum DepType {
    /// Code dependency: The cell provides code directly
    #[default]
    Code = 0,
    /// Dependency group: The cell bundles several cells as its members
    /// which will be expanded inside `cell_deps`.
    DepGroup = 1,
}
```

**File:** script/src/verify.rs (L427-444)
```rust
    fn verify_script_group(
        &self,
        group: &ScriptGroup,
        max_cycles: Cycle,
    ) -> Result<Cycle, ScriptError> {
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
        {
            let verifier = TypeIdSystemScript {
                rtx: &self.tx_data.rtx,
                script_group: group,
                max_cycles,
            };
            verifier.verify()
        } else {
            self.run(group, max_cycles)
        }
    }
```
