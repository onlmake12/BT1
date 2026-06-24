Audit Report

## Title
Unbounded Byte-by-Byte C-String Scan in `Debugger` Syscall With Post-Loop Cycle Accounting — (`File: script/src/syscalls/debugger.rs`)

## Summary
`Debugger::ecall` reads VM memory byte-by-byte into a host-side `Vec<u8>` with no length cap, charging cycles only after the loop completes via `add_cycles_no_checking`. Because `RISCV_MAX_MEMORY` is 4 MiB and the syscall is registered unconditionally for all script versions, a script can force the verifying node to perform up to 4,194,304 individual `load8` calls and a 4 MiB heap allocation per invocation before any cycle accounting occurs. Within the 70 M-cycle budget, this pattern can be repeated approximately 66 times per transaction, causing ~264 MiB of byte-by-byte host-side reads for a single accepted transaction.

## Finding Description
In `script/src/syscalls/debugger.rs` (L39–54), `Debugger::ecall` implements syscall `2177`:

```rust
let mut addr = machine.registers()[A0].to_u64();
let mut buffer = Vec::new();

loop {
    let byte = machine.memory_mut().load8(&Mac::REG::from_u64(addr))?.to_u8();
    if byte == 0 { break; }
    buffer.push(byte);
    addr = checked_add_addr(addr, 1)?;
}

machine.add_cycles_no_checking(transferred_byte_cycles(buffer.len() as u64))?;
```

There is no cap on `buffer.len()`. The loop terminates only on a null byte or `checked_add_addr` overflow (i.e., address wrapping past `u64::MAX`). The VM's memory is bounded to `RISCV_MAX_MEMORY` (4 MiB), so `load8` will return `MemOutOfBound` at that boundary — but not before the host has performed up to 4,194,304 individual memory-lookup calls.

`add_cycles_no_checking` (by design) does not enforce `max_cycles` mid-call; it only increments the counter. The cycle limit is enforced by the VM's instruction execution loop, which is not running during the syscall. A script that has 1 cycle remaining can still trigger the full 4 MiB scan before the cycle check fires on the next instruction.

`transferred_byte_cycles` charges 1 cycle per 4 bytes (`BYTES_PER_CYCLE = 4`), so 4 MiB costs 1,048,576 cycles. With a 70 M-cycle budget, a script can invoke syscall `2177` approximately 66 times with 4 MiB strings and remain within budget, causing the node to perform ~264 M `load8` calls and ~264 MiB of heap allocation to verify a single transaction.

The `Debugger` syscall is registered unconditionally for all script versions in `generate_ckb_syscalls` (L32 of `generator.rs`), with no version gate. The default `debug_printer` in production is a no-op closure (L76–82 of `verify.rs`), but the byte-by-byte scan and heap allocation run regardless of what the printer does.

By contrast, the `Exec` syscall (L158–172 of `exec.rs`) also reads C-strings byte-by-byte but enforces `MAX_ARGV_LENGTH` before accumulating further. `Debugger` has no equivalent guard.

## Impact Explanation
This is a bad design that can cause CKB network congestion with relatively low cost. An attacker submits transactions whose scripts call `ckb_debug` repeatedly with maximally-sized non-null memory regions. Each such transaction forces every verifying node to perform O(RISCV_MAX_MEMORY) byte-by-byte host reads and heap allocations per invocation, multiplied by the number of invocations that fit within the cycle budget. Under sustained submission of such transactions, node CPU and allocator pressure during script verification increases disproportionately relative to what the cycle model implies, slowing block template generation and transaction pool admission. This fits the allowed impact: **High — bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attacker requires only the ability to submit a transaction — an unprivileged operation available to any network participant. No special key, role, or majority hashpower is needed. The `Debugger` syscall is part of the standard CKB syscall ABI (number `2177`), available to scripts of all versions. Constructing the malicious script requires only filling VM memory with non-zero bytes and issuing the syscall. The attack is repeatable across many transactions and requires no victim interaction.

## Recommendation
1. **Add a hard length cap** before the loop (e.g., `MAX_DEBUG_STRING_LEN = 4096` bytes). Return an error or silently truncate if the string exceeds this limit, consistent with how `Exec` enforces `MAX_ARGV_LENGTH`.
2. **Replace the byte-by-byte loop** with a bounded `load_bytes` call up to the cap, eliminating both the unbounded `Vec` growth and the per-byte `load8` overhead.
3. **Charge cycles before or inside the loop** (or pre-charge a conservative upper bound) so that the cycle budget is enforced proportionally to host work performed, rather than after the fact.

## Proof of Concept
A RISC-V script targeting CKB-VM:

```c
#include "ckb_syscalls.h"
static uint8_t bomb[4 * 1024 * 1024 - 1];

int main() {
    __builtin_memset(bomb, 0xFF, sizeof(bomb));
    bomb[sizeof(bomb) - 1] = 0;
    // Repeat ~66 times to stay within 70M cycle budget
    for (int i = 0; i < 66; i++) {
        ckb_debug((const char *)bomb);
    }
    return 0;
}
```

When this script is executed during transaction verification, each `ckb_debug` call causes `Debugger::ecall` to iterate ~4 MiB byte-by-byte, allocate a 4 MiB `Vec<u8>`, perform `String::from_utf8` on 4 MiB, and only then charge ~1,048,576 cycles. The transaction stays within the 70 M-cycle budget and is accepted, while the node performs ~264 MiB of byte-by-byte host reads and heap allocations to verify it.