### Title
Unbounded `inherited_fds` Loop in `Spawn` Syscall Charges No Cycles and Enforces No Count Limit Before Collection - (File: `script/src/syscalls/spawn.rs`)

---

### Summary

The `Spawn` syscall's `ecall` handler reads the caller-controlled `inherited_fds` null-terminated array from VM memory in an unbounded loop with no per-iteration cycle charge and no count cap. The `MAX_FDS = 64` limit is only enforced later, in the scheduler, after the full uncapped collection is already complete. A malicious script author can supply a large non-zero fd array to force the node to perform O(N) host-side memory reads without consuming any VM cycles, amplifying host CPU and heap allocation cost beyond what the cycle budget implies.

---

### Finding Description

In `script/src/syscalls/spawn.rs`, the `ecall` implementation for the `SPAWN` syscall reads the `inherited_fds` pointer from the `spawn_args_t` struct and then iterates over VM memory in a `loop` until it finds a zero terminator:

```rust
let mut fds = vec![];
if fds_addr != 0 {
    loop {
        let fd = machine
            .memory_mut()
            .load64(&Mac::REG::from_u64(fds_addr))?
            .to_u64();
        if fd == 0 {
            break;
        }
        fds.push(Fd(fd));
        fds_addr = checked_add_addr(fds_addr, 8)?;
    }
}
``` [1](#0-0) 

The loop has three termination conditions: (1) a zero fd value is found, (2) `checked_add_addr` overflows and returns `VMError::MemOutOfBound`, or (3) `load64` hits an unmapped page. None of these conditions enforce the intended `MAX_FDS = 64` limit during collection. The `fds` `Vec` can grow to an arbitrary size.

The `MAX_FDS` limit is only checked later, inside `Scheduler::process_message_box`, when the `Message::Spawn` message is processed:

```rust
if self.fds.len() as u64 >= MAX_FDS {
    ...
    set_register(A0, MAX_FDS_CREATED);
    continue;
}
``` [2](#0-1) 

That check guards against creating new pipes, not against the number of fds passed to `spawn`. The fd ownership check that does iterate `args.fds` is:

```rust
if args.fds.iter().any(|fd| self.fds.get(fd) != Some(&vm_id)) {
``` [3](#0-2) 

This iterates the entire uncapped `args.fds` vec collected by the loop.

**Contrast with `Exec` syscall**: The `Exec` syscall's argv loop has an explicit `MAX_ARGV_LENGTH` (1 MiB) guard that aborts early:

```rust
if argv_length > MAX_ARGV_LENGTH {
    return Err(VMError::Unexpected(ARGV_TOO_LONG_TEXT.to_string()));
}
``` [4](#0-3) 

No equivalent guard exists in the `Spawn` fd-reading loop.

The `MAX_FDS` constant is defined as 64: [5](#0-4) 

---

### Impact Explanation

A malicious script author submits a transaction whose lock/type script calls `ckb_spawn` with an `inherited_fds` pointer referencing a large region of VM memory filled with non-zero values and no zero terminator until near the memory boundary. The node's script verifier enters the unbounded loop and performs up to `RISCV_MAX_MEMORY / 8` host-side `load64` calls (up to ~512 million iterations for a 4 GB address space) without charging a single VM cycle for those reads. Each iteration also pushes an `Fd` onto a heap-allocated `Vec`, causing unbounded heap growth. The cycle limit (`max_block_cycles`) does not protect against this because no cycles are charged inside the loop. The result is disproportionate host CPU and memory consumption per transaction, enabling a resource exhaustion / DoS attack against validating nodes.

---

### Likelihood Explanation

Any unprivileged script author can submit a transaction to the network. The script only needs to write non-zero values to a large region of its 4 GB address space before calling `ckb_spawn`. Writing to memory does consume VM cycles, but the amplification ratio (host `load64` calls per VM cycle) is favorable to the attacker because host memory reads are cheap relative to the cycle accounting overhead. The attack is repeatable across many transactions and requires no privileged access, leaked keys, or majority hashpower.

---

### Recommendation

Add a count cap inside the fd-reading loop, consistent with `MAX_FDS`:

```rust
let mut fds = vec![];
if fds_addr != 0 {
    loop {
        if fds.len() as u64 >= MAX_FDS {
            // Return an error code to the script instead of continuing
            machine.set_register(A0, Mac::REG::from_u8(MAX_FDS_CREATED));
            return Ok(true);
        }
        let fd = machine
            .memory_mut()
            .load64(&Mac::REG::from_u64(fds_addr))?
            .to_u64();
        if fd == 0 {
            break;
        }
        fds.push(Fd(fd));
        fds_addr = checked_add_addr(fds_addr, 8)?;
    }
}
```

This mirrors the fix recommended in M-07: bound the loop by the already-established maximum (`MAX_FDS`) at the point of collection, not after the fact.

---

### Proof of Concept

1. Author a RISC-V script that:
   - Allocates and writes `0xFFFFFFFF_FFFFFFF1` (non-zero) to every 8-byte slot across a large region of its heap (e.g., 64 MB), consuming cycles proportional to the write count.
   - Calls `ckb_spawn(0, CKB_SOURCE_CELL_DEP, 0, 0, &spgs)` with `inherited_fds` pointing to the start of that region and no zero terminator until the end.
2. Submit the transaction to a CKB node.
3. The node enters `Spawn::ecall` and executes the unbounded loop, performing millions of `load64` calls and `Vec::push` operations without charging any VM cycles.
4. The node's CPU and heap usage spike far beyond what the declared cycle count would predict, while the cycle limit is never exceeded from the VM's perspective.

The `Exec` syscall's `MAX_ARGV_LENGTH` guard (`script/src/syscalls/exec.rs` line 168) demonstrates that the CKB codebase already recognises this class of risk for argv; the same protection is absent for `inherited_fds` in `Spawn`. [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

**File:** script/src/syscalls/spawn.rs (L92-105)
```rust
        let mut fds = vec![];
        if fds_addr != 0 {
            loop {
                let fd = machine
                    .memory_mut()
                    .load64(&Mac::REG::from_u64(fds_addr))?
                    .to_u64();
                if fd == 0 {
                    break;
                }
                fds.push(Fd(fd));
                fds_addr = checked_add_addr(fds_addr, 8)?;
            }
        }
```

**File:** script/src/scheduler.rs (L34-38)
```rust
pub const MAX_VMS_COUNT: u64 = 16;
/// The maximum number of instantiated VMs.
pub const MAX_INSTANTIATED_VMS: usize = 4;
/// The maximum number of fds.
pub const MAX_FDS: u64 = 64;
```

**File:** script/src/scheduler.rs (L525-525)
```rust
                    if args.fds.iter().any(|fd| self.fds.get(fd) != Some(&vm_id)) {
```

**File:** script/src/scheduler.rs (L595-600)
```rust
                    if self.fds.len() as u64 >= MAX_FDS {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_FDS_CREATED));
                        continue;
```

**File:** script/src/syscalls/exec.rs (L154-173)
```rust
        let argc = machine.registers()[A4].to_u64();
        let mut addr = machine.registers()[A5].to_u64();
        let mut argv = Vec::new();
        let mut argv_length: u64 = 0;
        for _ in 0..argc {
            let target_addr = machine.memory_mut().load64(&Mac::REG::from_u64(addr))?;
            let cstr = load_c_string_byte_by_byte(machine.memory_mut(), &target_addr)?;
            let cstr_len = cstr.len();
            argv.push(cstr);

            // Number of argv entries should also be considered
            argv_length = argv_length
                .saturating_add(8)
                .saturating_add(cstr_len as u64);
            if argv_length > MAX_ARGV_LENGTH {
                return Err(VMError::Unexpected(ARGV_TOO_LONG_TEXT.to_string()));
            }

            addr = checked_add_addr(addr, 8)?;
        }
```
