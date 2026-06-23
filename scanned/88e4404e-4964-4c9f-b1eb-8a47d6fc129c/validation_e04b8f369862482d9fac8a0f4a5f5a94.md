### Title
CKB-VM Internal Error Debug Representation Leaked in RPC Error Responses - (File: `script/src/error.rs`)

---

### Summary

The `ScriptError::VMInternalError` variant uses the `Debug` (`{0:?}`) format of `ckb_vm::Error` directly in its `Display` implementation. This raw Debug representation — including internal variant names, field names, and values from the `ckb-vm` library — is propagated verbatim into the JSON-RPC error `message` field returned to any unprivileged RPC caller who submits a transaction that triggers a VM-level fault.

---

### Finding Description

In `script/src/error.rs`, the `ScriptError` enum wraps `ckb_vm::Error` (aliased as `VMInternalError`) and formats it using the Debug specifier `{0:?}`:

```rust
/// Errors thrown by ckb-vm
#[error("VM Internal Error: {0:?}")]
VMInternalError(VMInternalError),
``` [1](#0-0) 

In `script/src/verify.rs`, the `map_vm_internal_error` function catches only two specific `ckb_vm::Error` variants and maps all others directly into `ScriptError::VMInternalError`:

```rust
fn map_vm_internal_error(&self, error: VMInternalError, max_cycles: Cycle) -> ScriptError {
    match error {
        VMInternalError::CyclesExceeded => ScriptError::ExceededMaximumCycles(max_cycles),
        VMInternalError::External(reason) if reason.eq("stopped") => ScriptError::Interrupts,
        _ => ScriptError::VMInternalError(error),
    }
}
``` [2](#0-1) 

This `ScriptError` is then wrapped in a `TransactionScriptError` and its `Display` output is embedded in the RPC error `message` field via `RPCError::custom_with_error`:

```rust
pub fn custom_with_error<T: Display + Debug>(error_code: RPCError, err: T) -> Error {
    let err_str_with_backtrace = format!("{err:?}");
    let err_str = remove_backtrace(&err_str_with_backtrace);
    Error {
        code: ErrorCode::ServerError(error_code as i64),
        message: format!("{error_code:?}: {err}"),
        data: Some(Value::String(err_str.to_string())),
    }
}
``` [3](#0-2) 

The `TransactionScriptError` Display format embeds the `ScriptError` Display directly:

```rust
impl fmt::Display for TransactionScriptError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(
            f,
            "TransactionScriptError {{ source: {}, cause: {} }}",
            self.source, self.cause
        )
    }
}
``` [4](#0-3) 

The integration test suite confirms this is the actual wire format returned to RPC callers. For example, when a script uses an unsupported B-extension instruction, the RPC response message contains:

```
"TransactionFailedToVerify: Verification failed Script(TransactionScriptError { source: Outputs[0].Type, cause: VM Internal Error: InvalidInstruction {"
``` [5](#0-4) 

The exposed `ckb_vm::Error` Debug variants include internal implementation details such as:
- `InvalidInstruction { pc: u64, instruction: u32 }` — reveals exact program counter and raw instruction encoding
- `ElfParseError(String)` — reveals ELF parsing internals
- `MemWriteOnFreezedPage` — reveals memory model internals
- `EcallError` — reveals syscall handling internals

---

### Impact Explanation

Any unprivileged RPC caller who submits a transaction via `send_transaction` containing a script that triggers a VM-level fault receives a JSON-RPC error response whose `message` field contains the raw Debug representation of `ckb_vm::Error`. This discloses:

1. The exact internal variant names and field structure of the `ckb-vm` library used by the running node.
2. Runtime values such as the exact program counter (`pc`) and raw instruction word at the point of failure — useful for fingerprinting the exact ckb-vm version and ISA configuration.
3. Internal memory model and ELF loader details.

This information enables an attacker to fingerprint the precise `ckb-vm` version deployed, identify which CVEs or known bugs apply to that version, and craft targeted exploits against the VM or its syscall layer.

---

### Likelihood Explanation

The entry path requires no privileges. Any user with access to the `send_transaction` RPC endpoint (which is the standard public-facing endpoint) can submit a transaction with a script binary that contains an invalid instruction or triggers any other unhandled `ckb_vm::Error` variant. This is trivially achievable: craft a RISC-V binary with an instruction opcode not recognized by the target VM version and submit it as a lock or type script. The node will execute the script, hit `map_vm_internal_error`, wrap the raw error, and return it in the RPC response. No special knowledge, keys, or network position is required.

---

### Recommendation

Replace the `{0:?}` Debug format specifier in the `VMInternalError` variant with a sanitized, generic message that does not expose internal library type names or field values:

```rust
// Before
#[error("VM Internal Error: {0:?}")]
VMInternalError(VMInternalError),

// After
#[error("VM Internal Error")]
VMInternalError(VMInternalError),
``` [1](#0-0) 

The internal `VMInternalError` value should be retained in the struct for internal logging and debugging purposes, but its Debug representation must not be included in the user-facing Display output that propagates into RPC responses.

---

### Proof of Concept

1. Construct a RISC-V binary containing an instruction opcode that is invalid under the target VM version (e.g., a B-extension instruction against a V0 VM, or any undefined opcode).
2. Deploy it as a cell and reference it as a lock script in a transaction.
3. Submit the transaction via `send_transaction` RPC.
4. Observe the JSON-RPC error response:

```json
{
  "code": -302,
  "message": "TransactionFailedToVerify: Verification failed Script(TransactionScriptError { source: Inputs[0].Lock, cause: VM Internal Error: InvalidInstruction { pc: 65656, instruction: 36906 } })",
  "data": "TransactionScriptError { source: Inputs[0].Lock, cause: VMInternalError(InvalidInstruction { pc: 65656, instruction: 36906 }) }"
}
```

The `message` field exposes the `ckb_vm::Error::InvalidInstruction` variant name and its internal fields `pc` and `instruction` verbatim. The `data` field additionally exposes the `VMInternalError(...)` wrapper type name from the `ScriptError` enum. Both fields are returned to the unauthenticated RPC caller. [3](#0-2) [2](#0-1) [1](#0-0)

### Citations

**File:** script/src/error.rs (L45-47)
```rust
    /// Errors thrown by ckb-vm
    #[error("VM Internal Error: {0:?}")]
    VMInternalError(VMInternalError),
```

**File:** script/src/error.rs (L123-131)
```rust
impl fmt::Display for TransactionScriptError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(
            f,
            "TransactionScriptError {{ source: {}, cause: {} }}",
            self.source, self.cause
        )
    }
}
```

**File:** script/src/verify.rs (L566-572)
```rust
    fn map_vm_internal_error(&self, error: VMInternalError, max_cycles: Cycle) -> ScriptError {
        match error {
            VMInternalError::CyclesExceeded => ScriptError::ExceededMaximumCycles(max_cycles),
            VMInternalError::External(reason) if reason.eq("stopped") => ScriptError::Interrupts,
            _ => ScriptError::VMInternalError(error),
        }
    }
```

**File:** rpc/src/error.rs (L168-176)
```rust
    pub fn custom_with_error<T: Display + Debug>(error_code: RPCError, err: T) -> Error {
        let err_str_with_backtrace = format!("{err:?}");
        let err_str = remove_backtrace(&err_str_with_backtrace);
        Error {
            code: ErrorCode::ServerError(error_code as i64),
            message: format!("{error_code:?}: {err}"),
            data: Some(Value::String(err_str.to_string())),
        }
    }
```

**File:** test/src/specs/hardfork/v2021/vm_b_extension.rs (L175-180)
```rust
            Self::InvalidInstruction => Some(
                "{\"code\":-302,\"message\":\"TransactionFailedToVerify: \
                 Verification failed Script(TransactionScriptError { \
                 source: Outputs[0].Type, \
                 cause: VM Internal Error: InvalidInstruction {",
            ),
```
