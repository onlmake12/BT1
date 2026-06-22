### Title
Unprivileged Child VM Can Steal Sibling Exit Codes via Unchecked `wait` Syscall — (`script/src/scheduler.rs`)

---

### Summary

The CKB-VM `Scheduler` stores per-VM exit codes in a shared `terminated_vms` map. The `wait` syscall accepts an arbitrary `target_id` from the calling VM's register without verifying any parent-child relationship. Any VM within the same scheduler can call `wait` on any other VM's ID, allowing a malicious spawned child to consume a sibling's exit code from the shared map before the legitimate parent reads it. This is a direct analog to the MIMOProxy delegatecall storage collision: untrusted code (malicious child VM) manipulates shared authorization-relevant state (`terminated_vms`) that it has no business touching.

---

### Finding Description

The `Scheduler` struct maintains a shared `terminated_vms: BTreeMap<VmId, i8>` map that stores exit codes of terminated child VMs for later retrieval by waiting parents. [1](#0-0) 

When a VM calls the `wait` syscall, the `target_id` is taken directly from register `A0` — fully attacker-controlled — with no validation: [2](#0-1) 

The scheduler's `process_message_box` then handles the `Wait` message. It checks whether the target is in `terminated_vms`, and if so, delivers the exit code **and removes the entry** via `retain`: [3](#0-2) 

There is **no parent-child relationship tracking** anywhere in the `Scheduler`. The `fds` and `inherited_fd` maps track fd ownership, but no `spawner_vm` or `parent_vm` map exists: [1](#0-0) 

The scheduler always runs the highest-ID runnable VM first: [4](#0-3) 

Because VM IDs are assigned sequentially starting from `FIRST_VM_ID`, a malicious child spawned later (higher ID) is **always scheduled before the root VM** (ID 0) when both are runnable. This makes the attack deterministic.

**Attack trace:**

1. Root VM (ID 0) spawns Child A (ID 1, legitimate verifier) and Child B (ID 2, malicious).
2. Child B stays runnable (does not block).
3. Child A terminates with exit code `0`; the scheduler sets `terminated_vms[1] = 0` and removes VM 1 from `states`. [5](#0-4) 

4. Next iteration: Child B (ID 2, highest runnable) executes and calls `wait(1)`.
5. `process_message_box` finds `terminated_vms[1] = 0`, delivers it to Child B, and **removes the entry** (`terminated_vms.retain(|id, _| id != &1)`).
6. Root VM (ID 0) then calls `wait(1)`: `terminated_vms` no longer contains `1`, and `states` no longer contains `1` (VM was removed at step 3), so the scheduler returns `WAIT_FAILURE` (error code 5). [6](#0-5) 

---

### Impact Explanation

**Exit code theft / authorization bypass:** If a lock script spawns a verifier child and uses its exit code to gate transaction validity (a common pattern in CKB's spawn-based script composition), a malicious sibling can steal the exit code. The parent receives `WAIT_FAILURE` instead of the verifier's result. Depending on how the parent handles this error, the outcome is either:
- Transaction rejection (DoS against the user's own transaction), or
- Incorrect control-flow if the parent script conflates `WAIT_FAILURE` with a non-zero exit code from the verifier.

**Induced deadlock / permanent script failure:** A malicious child can call `wait(0)` (targeting the root VM, which is still alive in `states`). If the root VM is simultaneously waiting for the malicious child, a circular wait is created. The scheduler detects no runnable VM and returns `Error::Unexpected("A deadlock situation has been reached!")`, causing the script to fail unconditionally. [7](#0-6) 

---

### Likelihood Explanation

- VM IDs are sequential and predictable; a child can call `ckb_process_id()` to learn its own ID and infer sibling IDs. [8](#0-7) 

- The scheduler's highest-ID-first scheduling policy guarantees that a later-spawned malicious child runs before the root VM after a sibling terminates, making the race deterministic, not probabilistic.
- The attack is reachable by any transaction submitter who can influence which cell dep is loaded as a child script — a realistic scenario for extensible lock scripts that accept user-provided modules (e.g., scripts that spawn a "plugin" from a transaction-specified cell dep index).
- No privileged access, key material, or majority hashpower is required.

---

### Recommendation

Track the spawner of each VM in the scheduler and enforce that a VM may only `wait` on VMs it directly spawned:

```rust
// Add to Scheduler struct:
spawner: BTreeMap<VmId, VmId>,  // child_vm_id -> parent_vm_id

// In Message::Spawn handler, after boot_vm:
self.spawner.insert(spawned_vm_id, vm_id);

// In Message::Wait handler, before processing:
if self.spawner.get(&args.target_id) != Some(&vm_id) {
    // return WAIT_FAILURE or a new UNAUTHORIZED_WAIT error
}
```

This mirrors the fix applied to MIMOProxy: move authorization state out of shared, untrusted-code-accessible storage and enforce ownership at the boundary.

---

### Proof of Concept

```c
// malicious_child.c — deployed as a cell dep
// Assumes it is spawned as VM 2; VM 1 is the legitimate verifier
#include "ckb_syscalls.h"

int main() {
    // Steal VM 1's exit code before the root VM reads it
    int8_t exit_code = 0;
    // Spin until VM 1 has terminated (wait returns SUCCESS)
    while (ckb_wait(1, &exit_code) != CKB_SUCCESS) {}
    // Root VM's wait(1) will now return WAIT_FAILURE
    return 0;
}
```

```c
// root_lock_script.c
// Spawns verifier (VM 1) and user-provided plugin (VM 2)
int main() {
    uint64_t pid_verifier, pid_plugin;
    ckb_spawn(VERIFIER_INDEX, CKB_SOURCE_CELL_DEP, 0, 0, &pid_verifier);
    ckb_spawn(PLUGIN_INDEX,   CKB_SOURCE_CELL_DEP, 0, 0, &pid_plugin);

    int8_t code = 0;
    int ret = ckb_wait(pid_verifier, &code);
    // ret == WAIT_FAILURE (5) if malicious_child ran first
    // code is undefined; authorization check is corrupted
    if (ret != CKB_SUCCESS || code != 0) return -1;
    return 0;
}
```

The malicious plugin (VM 2, higher ID) is scheduled before the root VM (VM 0) by the highest-ID-first policy, guaranteeing it consumes `terminated_vms[1]` before the root VM's `wait(1)` is processed.

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

**File:** script/src/syscalls/mod.rs (L70-70)
```rust
pub const WAIT_FAILURE: u8 = 5;
```

**File:** script/src/syscalls/mod.rs (L95-95)
```rust
pub const PROCESS_ID: u64 = 2603;
```
