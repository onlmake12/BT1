### Title
`terminated_vms` Entry Consumed by First Waiter Causes Subsequent `ckb_wait` Callers to Receive `WAIT_FAILURE` on a Legitimately Terminated VM — (`script/src/scheduler.rs`)

---

### Summary

In `Scheduler::process_message_box`, when a `Message::Wait` is processed and the target VM is found in `terminated_vms`, the entry is unconditionally removed via `terminated_vms.retain(|id, _| id != &args.target_id)` after the first waiter is satisfied. Any subsequent VM that calls `ckb_wait` on the same already-terminated target finds it absent from both `terminated_vms` and `states`, and receives `WAIT_FAILURE` — even though the target terminated legitimately.

---

### Finding Description

The scheduler runs one VM per iteration. After each VM yields or terminates, `process_message_box` is called to drain and process all queued messages. The relevant path in `process_message_box` for `Message::Wait` is:

```
script/src/scheduler.rs, lines 564–593
```

**Step 1 — VM A terminates** (Iteration N):

`iterate_process_results` is called. `process_message_box` runs first (message box is empty). Then A's termination is handled:

- `terminated_vms.insert(A_id, exit_code)` — A's exit code is stored. [1](#0-0) 
- `states.remove(&A_id)` — A is removed from the live states map. [2](#0-1) 

No VMs are in `VmState::Wait` for A yet, so no joining VMs are woken up. [3](#0-2) 

**Step 2 — VM B calls `ckb_wait(A_id, addr_b)`** (Iteration N+1):

`Wait::ecall` pushes `Message::Wait(B_id, {target_id: A_id, ...})` and returns `Err(VMError::Yield)`. [4](#0-3) 

`process_message_box` processes it:
- A is found in `terminated_vms` → B receives `SUCCESS` and the correct exit code.
- **`terminated_vms.retain(|id, _| id != &args.target_id)` removes A's entry entirely.** [5](#0-4) 

**Step 3 — VM C calls `ckb_wait(A_id, addr_c)`** (Iteration N+2):

`process_message_box` processes it:
- A is **not** in `terminated_vms` (removed in Step 2).
- A is **not** in `states` (removed in Step 1).
- The fallthrough branch fires: C receives `WAIT_FAILURE`. [6](#0-5) 

This violates the invariant that every VM calling `ckb_wait` on a legitimately terminated target must receive `SUCCESS` and the correct exit code.

---

### Impact Explanation

Any V2 script that spawns multiple child VMs and has more than one of them call `ckb_wait` on the same target — sequentially, after that target has already terminated — will observe incorrect return codes. The second (and any further) waiter receives `WAIT_FAILURE` instead of `SUCCESS`. Depending on how the script uses the return value, this can cause:

- A transaction that should be **accepted** to be **rejected** (script returns non-zero due to unexpected `WAIT_FAILURE`).
- Incorrect branching logic within the script, leading to unpredictable and non-deterministic-looking behavior across node implementations if the bug is ever patched mid-chain.

The scope explicitly covers "Incorrect implementation or behavior of CKB-VM or system scripts."

---

### Likelihood Explanation

The trigger requires only that a script author write a V2 script where two child VMs call `ckb_wait` on the same target after it has already exited. This is a natural and reasonable pattern (e.g., a coordinator waiting for a worker, then a logger also waiting for the same worker). No privileged access, no hash power, no key material is required. Any unprivileged transaction submitter can craft such a script.

---

### Recommendation

Do not remove the `terminated_vms` entry after the first waiter. Instead, keep the entry until it is no longer reachable by any live VM. One approach: only remove the entry when no other VM holds a `VmState::Wait` referencing the same `target_vm_id`, and no other runnable VM could still issue a wait on it. A simpler conservative fix is to never remove from `terminated_vms` during `process_message_box` — entries can be cleaned up when the scheduler itself terminates or when the entry is provably unreachable.

Replace line 575:
```rust
// REMOVE this line:
self.terminated_vms.retain(|id, _| id != &args.target_id);
```

with deferred cleanup once all possible waiters have been accounted for.

---

### Proof of Concept

A state-transition test in the CKB script execution environment:

1. Root VM spawns three child VMs: A, B, C.
2. A immediately exits with code `42`.
3. B calls `ckb_wait(A_id, &exit_b)` — receives `SUCCESS`, `exit_b == 42`. ✓
4. C calls `ckb_wait(A_id, &exit_c)` — **currently receives `WAIT_FAILURE`** due to the `retain` at line 575 having removed A's entry. ✗
5. Assert both B and C received `SUCCESS` and `exit_code == 42`. The assertion for C fails, confirming the bug.

The relevant code path is entirely within `script/src/scheduler.rs` `process_message_box` at lines 564–593, triggered by the `Wait::ecall` syscall in `script/src/syscalls/wait.rs`. [7](#0-6)

### Citations

**File:** script/src/scheduler.rs (L363-364)
```rust
            Ok(code) => {
                self.terminated_vms.insert(vm_id_to_run, code);
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

**File:** script/src/scheduler.rs (L401-402)
```rust
                    // Clear terminated VM states
                    self.states.remove(&vm_id_to_run);
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

**File:** script/src/syscalls/wait.rs (L40-50)
```rust
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
```
