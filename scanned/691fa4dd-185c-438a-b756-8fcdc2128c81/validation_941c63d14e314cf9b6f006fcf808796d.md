### Title
Unprivileged Child VM Can Steal Sibling's Exit Code via `ckb_wait` Without Parent Ownership Check — (`script/src/syscalls/wait.rs`, `script/src/scheduler.rs`)

---

### Summary

`Wait::ecall` accepts any `target_id` from the calling VM's register without verifying that the caller is the parent of that target. Combined with the single-consumer semantics of `terminated_vms` in `process_message_box`, a child VM can call `ckb_wait` on a sibling's PID, consume the sibling's exit code, and cause the legitimate parent's subsequent `wait` to return `WAIT_FAILURE`.

---

### Finding Description

**Entry point — `Wait::ecall`:**

`target_id` is read directly from register A0 and forwarded to the message box with no parent-child relationship check. [1](#0-0) 

**Dispatch — `process_message_box`, `Message::Wait` arm:**

When the message is processed, the scheduler checks only two things: (1) is `target_id` already in `terminated_vms`? (2) is `target_id` still in `states`? There is no check that `vm_id` (the caller) is the parent of `args.target_id`. [2](#0-1) 

Critically, when the target is found in `terminated_vms`, the entry is **consumed** (removed) on line 575: [3](#0-2) 

**No parent tracking exists anywhere in the scheduler.** A grep for `parent` in `scheduler.rs` returns zero matches. The only per-VM relationship tracked is `inherited_fd` (file descriptor ownership), not spawn parentage. [4](#0-3) 

**PID predictability:** VM IDs are assigned sequentially from `next_vm_id`. A child spawned first can trivially predict the PID of a sibling spawned immediately after. [5](#0-4) 

---

### Impact Explanation

The concrete exploit path:

1. Root spawns child A (PID=1), then child B (PID=2).
2. B terminates → `iterate_process_results` inserts B into `terminated_vms` and removes B from `states`. [6](#0-5) 
3. A calls `ckb_wait(2)` (B's PID, guessed sequentially or communicated via pipe).
4. `process_message_box` finds B in `terminated_vms`, returns `SUCCESS` + exit code to A, and **removes B from `terminated_vms`** (line 575).
5. Root calls `ckb_wait(2)` → B is absent from both `terminated_vms` and `states` → root receives `WAIT_FAILURE`. [7](#0-6) 

Root's control flow now diverges from its intended logic. If root's script uses the wait result to decide transaction validity (e.g., asserting the child succeeded), the transaction outcome is corrupted by the malicious sibling.

---

### Likelihood Explanation

- Fully reachable via CKB-VM script execution — an unprivileged script author deploys a lock/type script using the spawn syscall family.
- No privileged access, leaked keys, or majority hashpower required.
- PID guessing is trivial (sequential integers); alternatively, root can pass B's PID to A via a pipe, making the attack deterministic.
- The attack is locally testable and reproducible.

---

### Recommendation

Track the parent VM ID at spawn time (e.g., add a `parent: BTreeMap<VmId, VmId>` map in the scheduler). In `process_message_box`'s `Message::Wait` handler, reject the wait with `WAIT_FAILURE` if `vm_id` is not the recorded parent of `args.target_id`. This mirrors POSIX `waitpid` semantics where only the direct parent may wait on a child.

---

### Proof of Concept

```
Root VM:
  spawn(A)  → pid_a = 1
  spawn(B)  → pid_b = 2
  // B exits with code 42

Child A (malicious):
  // A knows pid_b = pid_a + 1 (sequential)
  rc = ckb_wait(pid_b, &exit_code)
  // rc == SUCCESS, exit_code == 42
  // B's entry is now removed from terminated_vms

Root VM (continued):
  rc = ckb_wait(pid_b, &exit_code)
  // rc == WAIT_FAILURE  ← root's wait fails
  // Root treats B as failed; transaction logic is corrupted
```

### Citations

**File:** script/src/syscalls/wait.rs (L37-49)
```rust
        let target_id = machine.registers()[A0].to_u64();
        let exit_code_addr = machine.registers()[A1].to_u64();
        machine.add_cycles_no_checking(SPAWN_YIELD_CYCLES_BASE)?;
        self.message_box
            .lock()
            .map_err(|e| VMError::Unexpected(e.to_string()))?
            .push(Message::Wait(
                self.id,
                WaitArgs {
                    target_id,
                    exit_code_addr,
                },
            ));
```

**File:** script/src/scheduler.rs (L98-100)
```rust
    /// Next vm id used by spawn.
    next_vm_id: VmId,
    /// Next fd used by pipe.
```

**File:** script/src/scheduler.rs (L103-113)
```rust
    states: BTreeMap<VmId, VmState>,
    /// Used to confirm the owner of fd.
    fds: BTreeMap<Fd, VmId>,
    /// Verify the VM's inherited fd list.
    inherited_fd: BTreeMap<VmId, Vec<Fd>>,
    /// Instantiated vms.
    instantiated: BTreeMap<VmId, (VmContext<DL>, M)>,
    /// Suspended vms.
    suspended: BTreeMap<VmId, Snapshot2<DataPieceId>>,
    /// Terminated vms.
    terminated_vms: BTreeMap<VmId, i8>,
```

**File:** script/src/scheduler.rs (L363-404)
```rust
            Ok(code) => {
                self.terminated_vms.insert(vm_id_to_run, code);
                // When root VM terminates, the execution stops immediately, we will purge
                // all non-root VMs, and only keep root VM in states.
                // When non-root VM terminates, we only purge the VM's own states.
                if vm_id_to_run == ROOT_VM_ID {
                    self.ensure_vms_instantiated(&[vm_id_to_run])?;
                    self.instantiated.retain(|id, _| *id == vm_id_to_run);
                    self.suspended.clear();
                    self.states.clear();
                    self.states.insert(vm_id_to_run, VmState::Terminated);
                } else {
                    let joining_vms: Vec<(VmId, u64)> = self
                        .states
                        .iter()
                        .filter_map(|(vm_id, state)| match state {
                            VmState::Wait {
                                target_vm_id,
                                exit_code_addr,
                            } if *target_vm_id == vm_id_to_run => Some((*vm_id, *exit_code_addr)),
                            _ => None,
                        })
                        .collect();
                    // For all joining VMs, update exit code, then mark them as
                    // runnable state.
                    for (vm_id, exit_code_addr) in joining_vms {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .memory_mut()
                            .store8(&Self::u64_to_reg(exit_code_addr), &Self::i8_to_reg(code))?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(SUCCESS));
                        self.states.insert(vm_id, VmState::Runnable);
                    }
                    // Close fds
                    self.fds.retain(|_, vm_id| *vm_id != vm_id_to_run);
                    // Clear terminated VM states
                    self.states.remove(&vm_id_to_run);
                    self.instantiated.remove(&vm_id_to_run);
                    self.suspended.remove(&vm_id_to_run);
```

**File:** script/src/scheduler.rs (L564-593)
```rust
                Message::Wait(vm_id, args) => {
                    if let Some(exit_code) = self.terminated_vms.get(&args.target_id).copied() {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine.inner_mut().memory_mut().store8(
                            &Self::u64_to_reg(args.exit_code_addr),
                            &Self::i8_to_reg(exit_code),
                        )?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(SUCCESS));
                        self.states.insert(vm_id, VmState::Runnable);
                        self.terminated_vms.retain(|id, _| id != &args.target_id);
                        continue;
                    }
                    if !self.states.contains_key(&args.target_id) {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(WAIT_FAILURE));
                        continue;
                    }
                    // Return code will be updated when the joining VM exits
                    self.states.insert(
                        vm_id,
                        VmState::Wait {
                            target_vm_id: args.target_id,
                            exit_code_addr: args.exit_code_addr,
                        },
                    );
                }
```
