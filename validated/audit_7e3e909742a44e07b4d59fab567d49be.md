All four cited code locations are confirmed in the repository. Let me verify the VMError-to-ScriptError conversion path as well.

All claims are verified against the actual code. The complete error propagation path is confirmed:

1. `Debugger::ecall` returns `Err(VMError::External("String from buffer ..."))` for non-UTF-8 input [1](#0-0) 
2. `map_vm_internal_error` in `verify.rs` only special-cases `CyclesExceeded` and `External("stopped")` — all other `VMError::External` variants fall through to `ScriptError::VMInternalError` [2](#0-1) 
3. `Debugger` is unconditionally registered for all script versions [3](#0-2) 
4. `DEBUG_PRINT_SYSCALL_NUMBER = 2177` is available in all versions [4](#0-3) 

---

Audit Report

## Title
`Debugger::ecall` Terminates VM with `VMInternalError` on Non-UTF-8 Debug Input — (`script/src/syscalls/debugger.rs`)

## Summary
`Debugger::ecall` calls `String::from_utf8(buffer)` on bytes read from VM memory and propagates failure as `VMError::External(...)` via `?`. This error is not caught by any special-case handler and maps to `ScriptError::VMInternalError`, causing the entire script execution to fail. Any script that passes a buffer containing non-UTF-8 bytes to syscall 2177 (`DEBUG_PRINT_SYSCALL_NUMBER`) will be terminated with a VM internal error rather than continuing execution, even if the script logic is otherwise correct.

## Finding Description
In `script/src/syscalls/debugger.rs` lines 55–56, after reading bytes from VM memory until a null terminator, the code attempts UTF-8 decoding:

```rust
let s = String::from_utf8(buffer)
    .map_err(|e| VMError::External(format!("String from buffer {e:?}")))?;
```

The `?` operator propagates `VMError::External("String from buffer ...")` out of `ecall`, which the ckb-vm runtime surfaces as a fatal VM error. In `script/src/verify.rs` lines 566–572, `map_vm_internal_error` only special-cases `VMInternalError::CyclesExceeded` (→ `ExceededMaximumCycles`) and `VMInternalError::External("stopped")` (→ `Interrupts`). The string `"String from buffer ..."` does not match `"stopped"`, so it falls through to `ScriptError::VMInternalError(error)`. This is distinct from `ScriptError::ValidationFailure`, which is the expected outcome for a script that returns a non-zero exit code. The `Debugger` syscall is registered unconditionally for all script versions (V0 and above) in `generate_ckb_syscalls`, so no version is exempt. The UTF-8 check executes before the `DebugPrinter` callback is invoked, so even a no-op printer does not prevent the failure.

## Impact Explanation
This is an incorrect implementation of a CKB system syscall, matching the allowed impact: **"Incorrect implementation or behavior of CKB-VM or system scripts" (High, 10001–15000 points)**. A script that uses syscall 2177 with arbitrary byte data — e.g., reading from cell data, witnesses, or script args — will be rejected by all nodes with `ScriptError::VMInternalError` instead of completing normally. Because the behavior is deterministic, all nodes reject identically (no consensus split), but a valid transaction is permanently unprocessable. Lock scripts that fail this way would prevent spending of cells, constituting a concrete asset freeze.

## Likelihood Explanation
Any script author who uses the debug syscall with data read from external inputs (cell data, witnesses, args) rather than hardcoded string literals can trigger this. The syscall is documented as a general-purpose debug print with no stated UTF-8 requirement. The trigger is trivial: write `[0xFF, 0x00]` to a stack buffer and call ecall 2177 with a pointer to it. No special privileges are required — any unprivileged script author deploying a script to CKB can encounter this.

## Recommendation
Replace `String::from_utf8` with `String::from_utf8_lossy`, which substitutes invalid bytes with `U+FFFD` instead of returning an error:

```rust
// script/src/syscalls/debugger.rs, lines 55-57
let s = String::from_utf8_lossy(&buffer);
(self.printer)(&self.sg_info.script_hash, s.as_ref());
```

This preserves the debug output intent while making the syscall robust to arbitrary byte sequences.

## Proof of Concept
A RISC-V script that writes `[0xFF, 0x00]` to a stack buffer and calls ecall 2177 with a pointer to it will terminate with `ScriptError::VMInternalError(External("String from buffer Utf8Error { ... }"))` instead of returning exit code 0. A unit test using `TransactionScriptsVerifier` with such a compiled script binary would confirm the incorrect rejection. Alternatively, a Rust unit test can directly call `Debugger::ecall` on a mock machine whose memory contains `[0xFF, 0x00]` at the address in register A0 and assert that the result is `Err(VMError::External(...))`.

### Citations

**File:** script/src/syscalls/debugger.rs (L55-56)
```rust
        let s = String::from_utf8(buffer)
            .map_err(|e| VMError::External(format!("String from buffer {e:?}")))?;
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

**File:** script/src/syscalls/generator.rs (L23-33)
```rust
    let mut syscalls: Vec<Box<dyn Syscalls<M>>> = vec![
        Box::new(LoadScriptHash::new(sg_data)),
        Box::new(LoadTx::new(sg_data)),
        Box::new(LoadCell::new(sg_data)),
        Box::new(LoadInput::new(sg_data)),
        Box::new(LoadHeader::new(sg_data)),
        Box::new(LoadWitness::new(sg_data)),
        Box::new(LoadScript::new(sg_data)),
        Box::new(LoadCellData::new(vm_context)),
        Box::new(Debugger::new(sg_data, debug_printer)),
    ];
```

**File:** script/src/syscalls/mod.rs (L101-101)
```rust
pub const DEBUG_PRINT_SYSCALL_NUMBER: u64 = 2177;
```
