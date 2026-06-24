Audit Report

## Title
Unprivileged Child VM Can Steal Sibling's Exit Code via `ckb_wait` Without Parent Ownership Check — (`script/src/syscalls/wait.rs`, `script/src/scheduler.rs`)

## Summary

`Wait::ecall` forwards any caller-supplied `target_id` to the scheduler's message box with no parent-child relationship check. The `Message::Wait` handler in `process_message_box` consumes and removes the target's entry from `terminated_vms` upon a successful match, regardless of whether the caller is the legitimate parent. A malicious child VM can therefore call `ckb_wait` on a sibling's PID, drain the sibling's exit-code entry, and cause the true parent's subsequent `ckb_wait` to return `WAIT_FAILURE`, corrupting the parent script's transaction-validation logic.

## Finding Description

**Entry point — `Wait::ecall`:**

`target_id` is read directly from register A0 and pushed into the shared message box with no ownership check: [1](#0-0) 

**Dispatch — `process_message_box`, `Message::Wait` arm:**

The handler checks only two conditions: (1) is `target_id` already in `terminated_vms`? (2) is `target_id` still in `states`? There is no check that `vm_id` (the caller) is the parent of `args.target_id`: [2](#0-1) 

Critically, on a successful match the entry is **consumed** (permanently removed) from `terminated_vms`: [3](#0-2) 

**No parent tracking in the scheduler:**

The scheduler struct tracks `inherited_fd` (file-descriptor ownership) but has no `parent` map recording spawn parentage: [4](#0-3) 

**Sequential, predictable PIDs:**

`boot_vm` assigns IDs by simple increment, making sibling PIDs trivially predictable: [5](#0-4) 

**Scheduling order amplifies the attack:**

The scheduler always runs the runnable VM with the **largest** ID first: [6](#0-5) 

A child spawned after the root (higher ID) therefore runs before the root, giving it a natural window to call `ckb_wait` on a sibling before the root does.

**Why the `joining_vms` fast-path does not mitigate this:**

When a VM terminates, `iterate_process_results` immediately delivers the exit code to any VM already in `VmState::Wait` for that target. However, it does **not** remove the entry from `terminated_vms` at that point: [7](#0-6) 

The `terminated_vms` entry is only removed inside `process_message_box` (line 575). If the root has not yet issued `ckb_wait(B)` when B terminates, B's entry remains in `terminated_vms` and is fully exposed to theft by any other running VM.

## Impact Explanation

This is an **incorrect implementation of CKB-VM syscall behavior** — specifically the `ckb_wait` syscall — matching the allowed impact: *"Incorrect implementation or behavior of CKB-VM or system scripts"* (High, 10001–15000 points).

A concrete consequence: any root script that spawns a user-supplied "plugin" child alongside a trusted child, then waits on the trusted child's exit code to decide transaction validity, can be made to receive `WAIT_FAILURE` instead of the real exit code. This allows a malicious plugin author to force the root script into an unintended code path — either accepting an invalid transaction or rejecting a valid one — without any privileged access.

## Likelihood Explanation

- Fully reachable by any unprivileged script author who can get their code spawned as a child VM (e.g., a "sub-lock" or "plugin" pattern).
- No leaked keys, majority hashpower, or victim mistakes required.
- PID guessing is trivial (sequential integers starting from `FIRST_VM_ID`); alternatively, the root can pass the sibling's PID via a pipe, making the attack deterministic.
- The scheduling policy (highest ID runs first) gives the malicious child a reliable execution window before the root.
- Locally testable and reproducible with a minimal two-child script.

## Recommendation

Track the parent VM ID at spawn time by adding a `parent: BTreeMap<VmId, VmId>` field to the `Scheduler` struct. In the `Message::Wait` handler inside `process_message_box`, before delivering the exit code, verify that `vm_id == parent[args.target_id]`; if not, set register A0 to `WAIT_FAILURE` and `continue`. This mirrors POSIX `waitpid` semantics where only the direct parent may reap a child. The `parent` map entry should be inserted in the `Message::Spawn` handler alongside the existing `inherited_fd` insertion: [8](#0-7) 

## Proof of Concept

```
// Root VM (ID=0):
spawn(code_B, &pid_b)   // pid_b = 1
spawn(code_A, &pid_a)   // pid_a = 2  (A is malicious, runs first due to higher ID)
ckb_wait(pid_b, &code)  // <-- root expects B's exit code here

// Child A (ID=2, malicious, runs before root because ID 2 > ID 0):
// A knows pid_b = pid_a - 1 (sequential)
rc = ckb_wait(pid_b, &stolen_code)
// rc == SUCCESS, stolen_code == B's real exit code
// B's entry is now removed from terminated_vms

// Root VM (continued):
// ckb_wait(pid_b) now finds B absent from both terminated_vms and states
// → returns WAIT_FAILURE
// Root's transaction-validation logic is corrupted
```

Minimal test: write a CKB script test (using `ckb-testtool` or the scheduler's `iterate` API) that spawns two children — one that exits immediately with code 42, and one that calls `ckb_wait` on the first child's PID — then asserts that the root's subsequent `ckb_wait` on the first child returns `WAIT_FAILURE` and not `SUCCESS`/42.

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

**File:** script/src/scheduler.rs (L337-343)
```rust
        let vm_id_to_run = self
            .states
            .iter()
            .rev()
            .filter(|(_, state)| matches!(state, VmState::Runnable))
            .map(|(id, _)| *id)
            .next();
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

**File:** script/src/scheduler.rs (L547-553)
```rust
                    // Move passed fds from spawner to spawnee
                    for fd in &args.fds {
                        self.fds.insert(*fd, spawned_vm_id);
                    }
                    // Here we keep the original version of file descriptors.
                    // If one fd is moved afterward, this inherited file descriptors doesn't change.
                    self.inherited_fd.insert(spawned_vm_id, args.fds.clone());
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

**File:** script/src/scheduler.rs (L1016-1017)
```rust
        let id = self.next_vm_id;
        self.next_vm_id += 1;
```
