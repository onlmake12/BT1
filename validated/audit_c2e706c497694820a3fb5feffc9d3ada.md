### Title
Off-by-One in Spawn VM Count Limit Allows Exceeding `MAX_VMS_COUNT` - (File: script/src/scheduler.rs)

### Summary
The `Scheduler` in `script/src/scheduler.rs` uses a strict `>` comparison instead of `>=` when checking whether the number of alive VMs has reached `MAX_VMS_COUNT` (16) before allowing a `spawn` syscall. This allows a script to create 17 concurrent VMs when the protocol limit is 16, violating the stated resource constraint.

### Finding Description
In `process_message_box`, when handling a `Message::Spawn`, the guard is:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
    // return MAX_VMS_SPAWNED error
}
```

`MAX_VMS_COUNT` is 16. When there are already exactly 16 alive VMs (`suspended + instantiated == 16`), the condition `16 > 16` evaluates to `false`, so the spawn proceeds and `boot_vm` creates a 17th VM. The correct check should be `>= MAX_VMS_COUNT`, which would reject the spawn when the count is already at the limit.

By contrast, the analogous `MAX_FDS` check on the very same file uses the correct `>=` operator:

```rust
if self.fds.len() as u64 >= MAX_FDS {
    // return MAX_FDS_CREATED error
}
```

The inconsistency confirms the `MAX_VMS_COUNT` check is erroneous. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
**Impact: Medium.** A script author can spawn one more VM than the protocol-defined maximum of 16, creating 17 concurrent VMs. This violates the consensus resource limit for CKB-VM scripts. While the cycle limit still bounds total computation, the extra VM consumes additional memory and scheduler state beyond what the protocol intends to allow. Because all nodes run the same code, there is no consensus split — all nodes will accept the 17-VM script — but the protocol invariant `alive_vms ≤ MAX_VMS_COUNT` is silently broken. [4](#0-3) 

### Likelihood Explanation
**Likelihood: High.** Any transaction sender can craft a script that spawns VMs up to the boundary. A script that spawns exactly 15 children (root + 15 = 16 alive) and then spawns one more will succeed instead of receiving `MAX_VMS_SPAWNED`. No special privileges are required; the entry path is a standard transaction submission with a lock or type script. [5](#0-4) 

### Recommendation
Change the comparison operator from `>` to `>=`:

```diff
- if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
+ if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

This makes the `MAX_VMS_COUNT` check consistent with the `MAX_FDS` check and correctly rejects a spawn when the alive VM count is already at the limit. [6](#0-5) 

### Proof of Concept
A script using the CKB spawn syscall can demonstrate this:

1. Root VM (VM 0) spawns VM 1, which spawns VM 2, … continuing until 15 children are alive (total: 16 VMs = root + 15 children).
2. Any of the alive VMs calls `ckb_spawn` again.
3. With the current `>` check: `16 > 16` is `false` → spawn succeeds, creating VM 16 (17th total). The syscall returns `SUCCESS` instead of `MAX_VMS_SPAWNED`.
4. With the corrected `>=` check: `16 >= 16` is `true` → spawn is correctly rejected with `MAX_VMS_SPAWNED`.

The existing test `check_spawn_max_vms_count` in `script/src/verify/tests/ckb_latest/features_since_v2023.rs` exercises the limit but does not specifically test the exact boundary of 16 alive VMs, leaving the off-by-one undetected. [7](#0-6)

### Citations

**File:** script/src/scheduler.rs (L34-38)
```rust
pub const MAX_VMS_COUNT: u64 = 16;
/// The maximum number of instantiated VMs.
pub const MAX_INSTANTIATED_VMS: usize = 4;
/// The maximum number of fds.
pub const MAX_FDS: u64 = 64;
```

**File:** script/src/scheduler.rs (L523-563)
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

                    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                    machine.inner_mut().memory_mut().store64(
                        &Self::u64_to_reg(args.process_id_addr),
                        &Self::u64_to_reg(spawned_vm_id),
                    )?;
                    machine
                        .inner_mut()
                        .set_register(A0, Self::u8_to_reg(SUCCESS));
                }
```

**File:** script/src/scheduler.rs (L594-601)
```rust
                Message::Pipe(vm_id, args) => {
                    if self.fds.len() as u64 >= MAX_FDS {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_FDS_CREATED));
                        continue;
                    }
```

**File:** script/src/scheduler.rs (L1014-1039)
```rust
    /// Boot a vm by given program and args.
    fn boot_vm(&mut self, location: &DataLocation, args: VmArgs) -> Result<VmId, Error> {
        let id = self.next_vm_id;
        self.next_vm_id += 1;
        let (context, mut machine) = self.create_dummy_vm(&id)?;
        let (program, _) = {
            let sc = context.snapshot2_context.lock().expect("lock");
            sc.load_data(&location.data_piece_id, location.offset, location.length)?
        };
        self.load_vm_program(&context, &mut machine, location, program, args)?;
        // Newly booted VM will be instantiated by default
        while self.instantiated.len() >= MAX_INSTANTIATED_VMS {
            // Instantiated is a BTreeMap, first_entry will maintain key order
            let id = *self
                .instantiated
                .first_entry()
                .ok_or_else(|| Error::Unexpected("Map should not be empty".to_string()))?
                .key();
            self.suspend_vm(&id)?;
        }

        self.instantiated.insert(id, (context, machine));
        self.states.insert(id, VmState::Runnable);

        Ok(id)
    }
```

**File:** script/src/verify/tests/ckb_latest/features_since_v2023.rs (L102-106)
```rust
#[test]
fn check_spawn_max_vms_count() {
    let result = simple_spawn_test("testdata/spawn_cases", &[10]);
    assert_eq!(result.is_ok(), SCRIPT_VERSION == ScriptVersion::V2);
}
```
