The bug is real and concrete. Here is the full analysis:

---

### Title
`terminated_vms` Entry Consumed by First Waiter, Causing Second Waiter to Receive `WAIT_FAILURE` on a Legitimately Terminated VM — (`script/src/scheduler.rs`)

### Summary

In `Scheduler::process_message_box`, when a `Message::Wait` is processed for a VM that has already terminated (i.e., its ID is present in `terminated_vms`), the handler writes the exit code, returns `SUCCESS` to the waiting VM, and then **removes** the target's entry from `terminated_vms` via `retain`. Any subsequent `ckb_wait` call on the same target VM — from a different child VM — finds the target absent from both `terminated_vms` and `states`, and incorrectly receives `WAIT_FAILURE`.

### Finding Description

The scheduler runs one VM per iteration. After each VM yields or terminates, `process_message_box` is called to drain and process all pending messages. The relevant code path for `Message::Wait` is: [1](#0-0) 

Line 575 is the root cause:

```rust
self.terminated_vms.retain(|id, _| id != &args.target_id);
```

This removes the terminated VM's entry from `terminated_vms` after the **first** `ckb_wait` call consumes it. When a second VM subsequently calls `ckb_wait` on the same target:

- Line 565: `terminated_vms.get(&args.target_id)` → **not found** (already removed)
- Line 578: `states.contains_key(&args.target_id)` → **not found** (removed when A terminated at lines 402–404)
- Line 582: the second waiter receives `WAIT_FAILURE` [2](#0-1) 

When A terminates, the `iterate_process_results` path correctly wakes any VMs **already** in `VmState::Wait{target: A_id}` at that moment: [3](#0-2) 

But it does **not** handle the case where a VM calls `ckb_wait` on A **after** A has already terminated and after the first waiter has consumed the `terminated_vms` entry.

### Impact Explanation

An unprivileged script author can craft a V2 script (V2 is required for `spawn`/`wait` syscalls, available since the Meepo hardfork) where:

1. Parent spawns child VMs A, B, and C.
2. A terminates quickly.
3. B calls `ckb_wait(A_id)` → processes in iteration N → A's entry removed from `terminated_vms` → B gets `SUCCESS`.
4. C calls `ckb_wait(A_id)` → processes in iteration N+1 → A absent from both maps → C gets `WAIT_FAILURE`.

C's `WAIT_FAILURE` is incorrect. Any script logic that depends on C successfully joining A will malfunction. This constitutes incorrect CKB-VM/system script behavior: a script that should succeed (all waiters on a legitimately terminated VM receive `SUCCESS`) instead fails, which can cause valid transactions to be rejected or script logic to branch incorrectly. [4](#0-3) 

### Likelihood Explanation

The scenario is reachable by any unprivileged script author deploying a V2 script on-chain. No special privileges, leaked keys, or majority hashpower are required. The trigger condition — two VMs calling `ckb_wait` on the same already-terminated VM in sequence — is a natural pattern in fan-out/fan-in script designs. It is locally testable and deterministically reproducible.

### Recommendation

Do not remove the entry from `terminated_vms` inside `process_message_box`. The entry should persist until it is no longer needed. One approach: track a reference count of how many VMs are waiting (or expected to wait) on each terminated VM, and only remove the entry when the count reaches zero. Alternatively, simply never remove entries from `terminated_vms` during `process_message_box` — the map is bounded by `MAX_VMS_COUNT` (16) and is not persisted across scheduler suspension (it is reconstructed), so memory pressure is not a concern. [5](#0-4) [6](#0-5) 

### Proof of Concept

State-transition test (pseudocode):

```
// V2 script: parent
pid_a = spawn(child_a)   // child_a exits immediately with code 42
pid_b = spawn(child_b)   // child_b calls ckb_wait(pid_a)
pid_c = spawn(child_c)   // child_c calls ckb_wait(pid_a)

// child_b and child_c both call ckb_wait(pid_a, &exit_code)
// Expected: both receive SUCCESS and exit_code == 42
// Actual:   child_b receives SUCCESS; child_c receives WAIT_FAILURE
```

Concretely: write a C test program using the CKB syscall interface, spawn three VMs, have A exit with a known code, have B and C both call `ckb_wait(A_id)` in sequence (B runs first due to scheduler ordering by largest ID), and assert both return values are `SUCCESS`. The test will fail because C receives `WAIT_FAILURE`.

### Citations

**File:** script/src/scheduler.rs (L34-34)
```rust
pub const MAX_VMS_COUNT: u64 = 16;
```

**File:** script/src/scheduler.rs (L113-113)
```rust
    terminated_vms: BTreeMap<VmId, i8>,
```

**File:** script/src/scheduler.rs (L375-398)
```rust
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
```

**File:** script/src/scheduler.rs (L564-576)
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
```

**File:** script/src/scheduler.rs (L578-583)
```rust
                    if !self.states.contains_key(&args.target_id) {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(WAIT_FAILURE));
                        continue;
```

**File:** script/src/syscalls/wait.rs (L33-51)
```rust
    fn ecall(&mut self, machine: &mut Mac) -> Result<bool, VMError> {
        if machine.registers()[A7].to_u64() != WAIT {
            return Ok(false);
        }
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
        Err(VMError::Yield)
    }
```
