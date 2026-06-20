### Title
Unbounded `inherited_fds` Loop in `Spawn` Syscall Without Proportional Cycle Charge - (File: script/src/syscalls/spawn.rs)

### Summary
The `Spawn` syscall (syscall number 2601) reads the caller-supplied `inherited_fds` null-terminated list from VM memory using an unbounded loop that charges only a fixed cycle cost regardless of how many file-descriptor entries are traversed. A malicious script author can craft a large FD array in VM memory to force the host to perform O(n) work — memory reads, Vec allocations, and BTreeMap operations — while consuming only the flat `SPAWN_EXTRA_CYCLES_BASE + SPAWN_YIELD_CYCLES_BASE` (≈ 100,800) cycles per call.

### Finding Description

In `script/src/syscalls/spawn.rs`, the `Spawn::ecall` implementation reads the `inherited_fds` list from the spawning VM's memory using a null-terminated loop with no upper-bound check on the number of entries: [1](#0-0) 

The loop terminates only when it reads a zero value or when `checked_add_addr` overflows (i.e., when the address wraps past the 64-bit address space). Because CKB-VM's memory is 4 MB, the maximum number of 8-byte FD entries that can be placed before a zero terminator is approximately 4 MB / 8 = **524,288 entries**.

The only cycle charges applied to the entire `Spawn` syscall are two fixed constants: [2](#0-1) 

`SPAWN_EXTRA_CYCLES_BASE = 100_000` and `SPAWN_YIELD_CYCLES_BASE = 800`: [3](#0-2) 

These are flat costs — they do not scale with the length of the `fds` list.

After the `Spawn` syscall yields, `process_message_box` in `scheduler.rs` performs additional O(n) host-side work on the collected `fds` Vec:

1. **Ownership check** — `args.fds.iter().any(|fd| self.fds.get(fd) != Some(&vm_id))` iterates the entire Vec.
2. **BTreeMap inserts** — `for fd in &args.fds { self.fds.insert(*fd, spawned_vm_id); }` — O(n log n).
3. **Vec clone** — `self.inherited_fd.insert(spawned_vm_id, args.fds.clone())` — O(n). [4](#0-3) 

Critically, the `MAX_FDS = 64` guard checks the **total system FD count** (`self.fds.len()`), not the length of the attacker-supplied list: [5](#0-4) 

So a script can supply a list of ~512K **invalid** FD values. The ownership check (`iter().any(...)`) will eventually return `INVALID_FD`, but only after the host has already traversed the entire list.

Contrast this with the V1 `Exec` syscall, which **does** have a `MAX_ARGV_LENGTH` guard that bounds total work: [6](#0-5) 

No equivalent guard exists for the `Spawn` syscall's FD list.

### Impact Explanation

A script author (unprivileged, reachable via any transaction submitted to the tx-pool or included in a block) can deploy a script that:

1. Fills 4 MB of VM memory with non-zero 8-byte values followed by a zero terminator (~512K entries).
2. Calls `Spawn` (syscall 2601) repeatedly, each time triggering ~512K host

### Citations

**File:** script/src/syscalls/spawn.rs (L92-104)
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
```

**File:** script/src/syscalls/spawn.rs (L133-134)
```rust
        machine.add_cycles_no_checking(SPAWN_EXTRA_CYCLES_BASE)?;
        machine.add_cycles_no_checking(SPAWN_YIELD_CYCLES_BASE)?;
```

**File:** script/src/syscalls/mod.rs (L105-107)
```rust
pub const EXEC_LOAD_ELF_V2_CYCLES_BASE: u64 = 75_000;
pub const SPAWN_EXTRA_CYCLES_BASE: u64 = 100_000;
pub const SPAWN_YIELD_CYCLES_BASE: u64 = 800;
```

**File:** script/src/scheduler.rs (L523-553)
```rust
                Message::Spawn(vm_id, args) => {
                    // All fds must belong to the correct owner
                    if args.fds.iter().any(|fd| self.fds.get(fd) != Some(&vm_id)) {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(INVALID_FD));
                        continue;
                    }
                    if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_VMS_SPAWNED));
                        continue;
                    }
                    let spawned_vm_id = self.boot_vm(
                        &args.location,
                        VmArgs::Reader {
                            vm_id,
                            argc: args.argc,
                            argv: args.argv,
                        },
                    )?;
                    // Move passed fds from spawner to spawnee
                    for fd in &args.fds {
                        self.fds.insert(*fd, spawned_vm_id);
                    }
                    // Here we keep the original version of file descriptors.
                    // If one fd is moved afterward, this inherited file descriptors doesn't change.
                    self.inherited_fd.insert(spawned_vm_id, args.fds.clone());
```

**File:** script/src/scheduler.rs (L595-601)
```rust
                    if self.fds.len() as u64 >= MAX_FDS {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_FDS_CREATED));
                        continue;
                    }
```

**File:** script/src/syscalls/exec.rs (L164-170)
```rust
            // Number of argv entries should also be considered
            argv_length = argv_length
                .saturating_add(8)
                .saturating_add(cstr_len as u64);
            if argv_length > MAX_ARGV_LENGTH {
                return Err(VMError::Unexpected(ARGV_TOO_LONG_TEXT.to_string()));
            }
```
