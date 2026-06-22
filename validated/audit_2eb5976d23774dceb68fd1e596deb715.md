The code is confirmed. Let me trace the full path precisely.

**Key code in `script/src/syscalls/debugger.rs`:** [1](#0-0) 

Lines 55–56 are the root cause:
```rust
let s = String::from_utf8(buffer)
    .map_err(|e| VMError::External(format!("String from buffer {e:?}")))?;
```

The `?` propagates `VMError::External(...)` out of `ecall`, terminating the VM with an error.

**Error mapping in `script/src/error.rs`:** [2](#0-1) 

`VMError::External` is wrapped into `ScriptError::VMInternalError`, not `ScriptError::ValidationFailure`.

**`Debugger` is unconditionally registered for all script versions:** [3](#0-2) 

`DEBUG_PRINT_SYSCALL_NUMBER` is available in every script version: [4](#0-3) 

---

### Title
`Debugger::ecall` Rejects Invalid UTF-8 Debug Strings as `VMInternalError`, Incorrectly Failing Scripts — (`script/src/syscalls/debugger.rs`)

### Summary
`Debugger::ecall` calls `String::from_utf8(buffer)` on bytes read from VM memory and propagates the `Err` case as `VMError::External`, which surfaces to callers as `ScriptError::VMInternalError`. Any script that passes a buffer containing non-UTF-8 bytes to syscall 2177 (`DEBUG_PRINT_SYSCALL_NUMBER`) will be terminated with a VM internal error rather than continuing execution or receiving an error code in a register.

### Finding Description
In `script/src/syscalls/debugger.rs` lines 55–56, after reading bytes from VM memory until a null terminator, the code attempts to decode them as UTF-8:

```rust
let s = String::from_utf8(buffer)
    .map_err(|e| VMError::External(format!("String from buffer {e:?}")))?;
```

If `buffer` contains any byte that is not valid UTF-8 (e.g., `0xFF`, `0x80`–`0xBF` in isolation, overlong sequences, etc.), `String::from_utf8` returns `Err`, which is converted to `VMError::External(...)` and immediately propagated via `?`. This terminates the VM with an error that maps to `ScriptError::VMInternalError` — not `ScriptError::ValidationFailure` — causing the transaction to be rejected with an unexpected internal error.

The `Debugger` syscall handler is unconditionally registered for all script versions (V0 and above) in `generate_ckb_syscalls`. The `String::from_utf8` check executes before the `DebugPrinter` callback is invoked, so even a no-op printer does not prevent the failure.

### Impact Explanation
A script that:
1. Reads arbitrary bytes from cell data, witnesses, or any external source into VM memory, and
2. Passes a pointer to those bytes to `DEBUG_PRINT_SYSCALL_NUMBER` for diagnostic output

will fail with `ScriptError::VMInternalError` if any byte is not valid UTF-8, even if the script's logic is otherwise correct and should return exit code 0. This can cause a valid transaction to be rejected by all nodes (the behavior is deterministic, so there is no consensus split — all nodes reject identically). Lock scripts that fail this way would prevent spending of cells.

### Likelihood Explanation
Any script author who uses the debug syscall with data read from external inputs (cell data, witnesses, args) rather than string literals risks triggering this. The syscall is documented as a general-purpose debug print, with no documented UTF-8 requirement. The bug is trivially reproducible: write `[0xFF, 0x00]` to memory and call ecall 2177.

### Recommendation
Replace `String::from_utf8` with `String::from_utf8_lossy`, which replaces invalid bytes with the Unicode replacement character (`U+FFFD`) instead of returning an error:

```rust
// script/src/syscalls/debugger.rs, line 55
let s = String::from_utf8_lossy(&buffer);
(self.printer)(&self.sg_info.script_hash, s.as_ref());
```

This preserves the debug output intent while making the syscall robust to arbitrary byte sequences.

### Proof of Concept
A RISC-V script that writes `[0xFF, 0x00]` to a stack buffer and calls ecall 2177 with a pointer to it will terminate with `ScriptError::VMInternalError("VM Internal Error: External(\"String from buffer ...\")")` instead of returning exit code 0. A unit test using `TransactionScriptsVerifier` with such a script binary would confirm the incorrect rejection.

### Citations

**File:** script/src/syscalls/debugger.rs (L42-56)
```rust
        loop {
            let byte = machine
                .memory_mut()
                .load8(&Mac::REG::from_u64(addr))?
                .to_u8();
            if byte == 0 {
                break;
            }
            buffer.push(byte);
            addr = checked_add_addr(addr, 1)?;
        }

        machine.add_cycles_no_checking(transferred_byte_cycles(buffer.len() as u64))?;
        let s = String::from_utf8(buffer)
            .map_err(|e| VMError::External(format!("String from buffer {e:?}")))?;
```

**File:** script/src/error.rs (L45-47)
```rust
    /// Errors thrown by ckb-vm
    #[error("VM Internal Error: {0:?}")]
    VMInternalError(VMInternalError),
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
