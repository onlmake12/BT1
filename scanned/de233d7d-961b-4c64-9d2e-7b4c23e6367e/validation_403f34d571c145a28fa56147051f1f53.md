### Title
Unbounded Host-Side Buffer Allocation in `Debugger` Syscall Without In-Loop Cycle Accounting — (`File: script/src/syscalls/debugger.rs`)

### Summary

The `Debugger` syscall (number `2177`) reads a C-string byte-by-byte from CKB-VM memory into a host-side `Vec<u8>` with no length limit, and charges cycles only **after** the loop completes. A malicious script author can craft a transaction script that points the syscall at a maximally-sized non-null region of VM memory, forcing the CKB node to perform up to 4 MiB of host-side allocation and iteration before any cycle accounting occurs. This is structurally identical to the "returndata gas bomb" in the reference report: the host does unbounded work before the resource limit is enforced.

---

### Finding Description

`Debugger::ecall` in `script/src/syscalls/debugger.rs` implements syscall `2177`:

```rust
let mut addr = machine.registers()[A0].to_u64();
let mut buffer = Vec::new();

loop {
    let byte = machine.memory_mut().load8(&Mac::REG::from_u64(addr))?.to_u8();
    if byte == 0 {
        break;
    }
    buffer.push(byte);
    addr = checked_add_addr(addr, 1)?;
}

machine.add_cycles_no_checking(transferred_byte_cycles(buffer.len() as u64))?;
``` [1](#0-0) 

There is no cap on `buffer.len()`. The loop runs until it finds a null byte or `checked_add_addr` overflows. Cycles are charged only after the loop exits via `add_cycles_no_checking`, which does not enforce the cycle limit mid-loop. [2](#0-1) 

Critically, `Debugger` is registered unconditionally for **all** script versions — it is not gated behind `ScriptVersion::V1` or `ScriptVersion::V2`:

```rust
let mut syscalls: Vec<Box<dyn Syscalls<M>>> = vec![
    ...
    Box::new(Debugger::new(sg_data, debug_printer)),
];
``` [3](#0-2) 

CKB-VM memory is bounded to `RISCV_MAX_MEMORY` (4 MiB). A script that fills its entire address space with non-zero bytes and then calls `ckb_debug` with a pointer to offset 0 forces the host to:

1. Iterate 4 MiB byte-by-byte via `load8` calls — all without decrementing the cycle counter.
2. Grow a host-side `Vec<u8>` to 4 MiB.
3. Only then charge `transferred_byte_cycles(4_194_304)` cycles.

If the script is near its cycle budget when it issues the call, the host completes the full 4 MiB scan before the cycle check fires. The script can repeat this pattern across multiple syscall invocations within a single transaction, multiplying the host-side work relative to what the cycle counter reflects at any given moment.

---

### Impact Explanation

Transaction verification in CKB is expected to be bounded by the declared cycle limit (`max_tx_verify_cycles = 70_000_000` by default). The `Debugger` syscall breaks this invariant: the host performs O(RISCV_MAX_MEMORY) work per invocation before the cycle budget is decremented. A malicious script author can craft a transaction that causes the verifying node to spend disproportionate CPU and memory resources during script execution, slowing block processing and transaction pool admission. Under sustained submission of such transactions, this degrades node throughput and can delay block template generation. [4](#0-3) 

---

### Likelihood Explanation

The attacker entry path requires only the ability to submit a transaction — an unprivileged operation available to any network participant. No special role, key, or majority hashpower is needed. The `Debugger` syscall is part of the standard CKB syscall ABI and is available to scripts of all versions. Constructing the malicious script requires only filling VM memory with non-zero bytes and issuing syscall `2177`. [5](#0-4) 

---

### Recommendation

1. **Add a hard length cap** before the loop, e.g. `MAX_DEBUG_STRING_LEN = 1024` or `4096` bytes. Return an error or silently truncate if the string exceeds this limit.
2. **Charge cycles inside the loop** (or pre-charge a conservative upper bound before entering the loop) so that the cycle budget is enforced proportionally to host work performed, consistent with how other syscalls handle transferred-byte costs.
3. Alternatively, replace the byte-by-byte loop with a bounded `load_bytes` call up to the cap, eliminating the unbounded `Vec` growth entirely.

---

### Proof of Concept

A RISC-V script targeting CKB-VM:

```c
#include "ckb_syscalls.h"

int main() {
    // Fill the first 4 MiB of stack/BSS with 0xFF (non-null)
    static uint8_t bomb[4 * 1024 * 1024 - 1];
    __builtin_memset(bomb, 0xFF, sizeof(bomb));
    bomb[sizeof(bomb) - 1] = 0; // null terminator at the very end

    // Syscall 2177: Debugger. Host iterates all 4 MiB before charging cycles.
    ckb_debug((const char *)bomb);
    return 0;
}
```

When this script is executed during transaction verification, `Debugger::ecall` iterates all ~4 MiB bytes, allocates a 4 MiB `Vec<u8>` on the host heap, and only then charges cycles. Repeating the call pattern within the cycle budget multiplies the host-side allocation and iteration work. [6](#0-5)

### Citations

**File:** script/src/syscalls/debugger.rs (L39-57)
```rust
        let mut addr = machine.registers()[A0].to_u64();
        let mut buffer = Vec::new();

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
        (self.printer)(&self.sg_info.script_hash, s.as_str());
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

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```

**File:** script/src/syscalls/mod.rs (L101-101)
```rust
pub const DEBUG_PRINT_SYSCALL_NUMBER: u64 = 2177;
```
