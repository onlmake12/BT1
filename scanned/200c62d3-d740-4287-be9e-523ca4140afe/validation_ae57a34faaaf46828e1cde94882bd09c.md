Audit Report

## Title
Unprivileged Child VM Can Consume Sibling Exit Codes via Unchecked `wait` Syscall — (`script/src/scheduler.rs`)

## Summary
The `Scheduler` stores terminated VM exit codes in a shared `terminated_vms: BTreeMap<VmId, i8>` map with no ownership tracking. The `wait` syscall accepts an arbitrary `target_id` from the calling VM's register A0 with no parent-child relationship check. Any runnable VM can consume any other VM's exit code from `terminated_vms`, causing the legitimate parent's subsequent `wait` call to receive `WAIT_FAILURE` instead of the actual exit code. This constitutes incorrect behavior of the CKB-VM scheduler.

## Finding Description

**Root cause — no ownership on `terminated_vms`:**

The `Scheduler` struct has no `spawner` map or any parent-child relationship tracking. [1](#0-0) 

The `wait` syscall reads `target_id` directly from register A0 with no validation: [2](#0-1) 

**Termination path — late-wait race window:**

When a non-root VM terminates, `iterate_process_results` immediately delivers the exit code to any VM already in `VmState::Wait` targeting it, then removes the terminated VM from `states`. If no VM is yet in `Wait` state (the parent hasn't called `wait` yet), the exit code is stored in `terminated_vms` and the VM is removed from `states`: [3](#0-2) 

**Consumption path — any VM can drain the entry:**

In `process_message_box`, the `Wait` handler checks `terminated_vms` for the target ID, delivers the exit code to the requesting VM, and **removes the entry** via `retain`. There is no check that the requesting VM is the spawner of the target: [4](#0-3) 

After the entry is removed, a subsequent `wait(target_id)` from the legitimate parent finds neither `terminated_vms[target_id]` nor `states[target_id]`, and receives `WAIT_FAILURE`: [5](#0-4) 

**Scheduling guarantee — attack is deterministic:**

`iterate_prepare_machine` always selects the highest-ID runnable VM: [6](#0-5) 

Since VM IDs are assigned sequentially, a malicious child spawned after the legitimate verifier always has a higher ID and is always scheduled before the root VM when both are runnable.

**Exact attack trace:**
1. Root VM (ID 0) spawns verifier Child A (ID 1) and malicious Child B (ID 2).
2. Child B stays runnable (does not block).
3. Child A terminates with exit code `0`. No VM is in `VmState::Wait { target_vm_id: 1 }` yet, so `terminated_vms[1] = 0` is set and VM 1 is removed from `states`.
4. Next iteration: Child B (ID 2, highest runnable) executes and calls `wait(1)`.
5. `process_message_box` finds `terminated_vms[1] = 0`, delivers it to Child B, removes the entry.
6. Root VM (ID 0) calls `wait(1)`: `terminated_vms` no longer contains `1`, `states` no longer contains `1` → scheduler returns `WAIT_FAILURE` to root VM.

## Impact Explanation

This is **incorrect implementation or behavior of CKB-VM** (High, 10001–15000 points). A malicious spawned child can corrupt the parent script's authorization logic by stealing a sibling verifier's exit code. Lock scripts that use spawn-based script composition — spawning a verifier child and gating transaction validity on its exit code — are directly affected. The parent receives `WAIT_FAILURE` (code 5) instead of the verifier's result, which either causes transaction rejection (DoS against the user's own transaction) or incorrect control flow if the parent conflates `WAIT_FAILURE` with a non-zero verifier exit code.

## Likelihood Explanation

- VM IDs are sequential and predictable; a child can call `ckb_process_id()` to learn its own ID and infer sibling IDs.
- The highest-ID-first scheduling policy makes the attack deterministic, not probabilistic — a later-spawned malicious child is guaranteed to run before the root VM after a sibling terminates.
- The attack is reachable by any transaction submitter who can influence which cell dep is loaded as a child script, a realistic scenario for extensible lock scripts that accept user-provided modules (e.g., scripts that spawn a "plugin" from a transaction-specified cell dep index).
- No privileged access, key material, or majority hashpower is required.

## Recommendation

Track the spawner of each VM in the scheduler and enforce that a VM may only `wait` on VMs it directly spawned:

```rust
// Add to Scheduler struct:
spawner: BTreeMap<VmId, VmId>,  // child_vm_id -> parent_vm_id

// In Message::Spawn handler, after boot_vm:
self.spawner.insert(spawned_vm_id, vm_id);

// In Message::Wait handler, before processing:
if self.spawner.get(&args.target_id) != Some(&vm_id) {
    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
    machine.inner_mut().set_register(A0, Self::u8_to_reg(WAIT_FAILURE));
    continue;
}
```

This enforces ownership at the scheduler boundary, preventing any VM from consuming exit codes it did not produce by spawning.

## Proof of Concept

```c
// malicious_child.c — deployed as a cell dep, spawned as VM 2
// VM 1 is the legitimate verifier
#include "ckb_syscalls.h"

int main() {
    int8_t exit_code = 0;
    // Spin until VM 1 has terminated (wait returns SUCCESS)
    while (ckb_wait(1, &exit_code) != CKB_SUCCESS) {}
    // Root VM's wait(1) will now return WAIT_FAILURE
    return 0;
}
```

```c
// root_lock_script.c
int main() {
    uint64_t pid_verifier, pid_plugin;
    ckb_spawn(VERIFIER_INDEX, CKB_SOURCE_CELL_DEP, 0, 0, &pid_verifier); // VM 1
    ckb_spawn(PLUGIN_INDEX,   CKB_SOURCE_CELL_DEP, 0, 0, &pid_plugin);   // VM 2

    int8_t code = 0;
    int ret = ckb_wait(pid_verifier, &code);
    // ret == WAIT_FAILURE (5) — authorization check corrupted
    if (ret != CKB_SUCCESS || code != 0) return -1;
    return 0;
}
```

The malicious plugin (VM 2, higher ID) is scheduled before the root VM (VM 0) by the highest-ID-first policy, guaranteeing it consumes `terminated_vms[1]` before the root VM's `wait(1)` is processed. This is reproducible as a unit test by constructing a scheduler with three VMs following the above pattern and asserting that the root VM's `wait` returns `WAIT_FAILURE`.

### Citations

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

**File:** script/src/scheduler.rs (L335-348)
```rust
    fn iterate_prepare_machine(&mut self) -> Result<(u64, &mut M), Error> {
        // Find a runnable VM that has the largest ID.
        let vm_id_to_run = self
            .states
            .iter()
            .rev()
            .filter(|(_, state)| matches!(state, VmState::Runnable))
            .map(|(id, _)| *id)
            .next();
        let vm_id_to_run = vm_id_to_run.ok_or_else(|| {
            Error::Unexpected("A deadlock situation has been reached!".to_string())
        })?;
        let (_context, machine) = self.ensure_get_instantiated(&vm_id_to_run)?;
        Ok((vm_id_to_run, machine))
```

**File:** script/src/scheduler.rs (L362-405)
```rust
        match result {
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
                }
```

**File:** script/src/scheduler.rs (L564-592)
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
```

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
