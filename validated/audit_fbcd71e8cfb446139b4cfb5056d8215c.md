### Title
Off-by-One in Spawn VM Count Allows Exceeding `MAX_VMS_COUNT` Limit — (`script/src/scheduler.rs`)

---

### Summary

`MAX_VMS_COUNT = 16` is defined as the maximum number of simultaneously active VMs in the CKB-VM scheduler. However, the spawn guard uses a strict `>` comparison instead of `>=`, allowing a script to create 17 concurrent VMs instead of the intended 16. This is a direct analog to the TraitForge finding: a defined maximum constant exists but is not correctly enforced at the increment/creation site.

---

### Finding Description

In `script/src/scheduler.rs`, two resource limits are defined:

```rust
/// The maximum number of VMs that can be created at the same time.
pub const MAX_VMS_COUNT: u64 = 16;
/// The maximum number of fds.
pub const MAX_FDS: u64 = 64;
``` [1](#0-0) 

When a `Spawn` message is processed, the guard is:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
    // return MAX_VMS_SPAWNED error to caller
    continue;
}
let spawned_vm_id = self.boot_vm(...)?;
``` [2](#0-1) 

Because the check is `> 16` (strictly greater than), it only rejects when the count is **already 17 or more**. When the count is exactly 16, `16 > 16` evaluates to `false`, so the spawn proceeds and the count becomes **17**. The limit is only enforced starting from the 18th VM.

By contrast, the `Pipe` (fd) guard uses the correct `>=` operator:

```rust
if self.fds.len() as u64 >= MAX_FDS {
    // return MAX_FDS_CREATED error
    continue;
}
``` [3](#0-2) 

This inconsistency confirms the spawn check is an off-by-one error. The fd check correctly enforces its limit at exactly `MAX_FDS`, while the VM spawn check allows one extra VM beyond `MAX_VMS_COUNT`.

The `boot_vm` function that is called after the guard also performs no independent upper-bound check on `next_vm_id` or the total VM count:

```rust
fn boot_vm(&mut self, location: &DataLocation, args: VmArgs) -> Result<VmId, Error> {
    let id = self.next_vm_id;
    self.next_vm_id += 1;
    ...
    self.instantiated.insert(id, (context, machine));
    self.states.insert(id, VmState::Runnable);
    Ok(id)
}
``` [4](#0-3) 

---

### Impact Explanation

A script author can craft a lock or type script that spawns VMs up to 17 simultaneously instead of the protocol-defined 16. The `MAX_VMS_COUNT` constant is a consensus-level resource parameter. Exceeding it means:

1. **Protocol invariant violated**: The stated maximum of 16 concurrent VMs is not enforced; scripts can operate with 17.
2. **Resource accounting skew**: Memory and scheduling overhead for 17 VMs is higher than the protocol intends to permit. Each VM carries snapshot state, instantiated machine state, and associated fd bookkeeping.
3. **Consensus parameter drift**: If a future implementation or light client enforces `>= MAX_VMS_COUNT` correctly, it would reject scripts that the current node accepts, creating a consensus divergence.

---

### Likelihood Explanation

Any unprivileged user who can deploy a CKB script (lock or type script) and submit a transaction referencing it can trigger this path. No special privilege, key, or majority hashpower is required. The `Spawn` syscall is a standard CKB-VM V2 feature available to all script authors. The attacker simply needs to write a script that spawns VMs in a loop until the 17th spawn succeeds where it should have been rejected.

---

### Recommendation

Change the spawn guard from strict `>` to `>=` to match the semantics of `MAX_VMS_COUNT` and the existing fd guard:

```rust
// Before (off-by-one — allows 17 VMs):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct — enforces exactly 16 VMs):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
``` [5](#0-4) 

This aligns the VM spawn guard with the fd guard pattern already used at line 595, and correctly enforces the protocol-defined `MAX_VMS_COUNT = 16` limit.

---

### Proof of Concept

A script targeting CKB-VM V2 (`ScriptVersion::V2`) can demonstrate the bypass:

```c
// Pseudocode for a CKB lock script
int main() {
    uint64_t pid;
    int spawned = 0;
    // Attempt to spawn 17 VMs (should fail at 17th under correct enforcement)
    for (int i = 0; i < 17; i++) {
        int ret = ckb_spawn(/* self */, &pid, /* no fds */);
        if (ret == CKB_SUCCESS) {
            spawned++;
        } else {
            // ret == MAX_VMS_SPAWNED (8)
            break;
        }
    }
    // Under the buggy code, spawned == 17 (not 16)
    // Under the fixed code, spawned == 15 (root + 15 children = 16 total)
    return spawned == 17 ? 0 : 1;
}
```

The root VM counts as 1, so 15 successful spawns produce 16 total VMs. With the off-by-one bug, a 16th spawn also succeeds (17 total), where `MAX_VMS_COUNT = 16` should have caused it to return `MAX_VMS_SPAWNED`. [6](#0-5)

### Citations

**File:** script/src/scheduler.rs (L33-38)
```rust
/// The maximum number of VMs that can be created at the same time.
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

**File:** script/src/scheduler.rs (L1015-1038)
```rust
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
```
