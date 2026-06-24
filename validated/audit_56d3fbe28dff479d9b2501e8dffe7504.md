Audit Report

## Title
Premature `terminated_vms` Eviction Causes `WAIT_FAILURE` for Second `ckb_wait` Caller on Same Target — (`script/src/scheduler.rs`)

## Summary

In `process_message_box`, when a `Message::Wait` is handled and the target VM is already in `terminated_vms`, the handler delivers `SUCCESS` to the first waiter and then unconditionally removes the target's entry via `retain`. Any subsequent VM that calls `ckb_wait` on the same target finds it absent from both `terminated_vms` and `states` and receives `WAIT_FAILURE` instead of `SUCCESS`. This constitutes incorrect CKB-VM behavior: a valid multi-waiter script is silently mis-validated on-chain.

## Finding Description

**Root cause — line 575:** [1](#0-0) 

After delivering `SUCCESS` to the first waiter, `self.terminated_vms.retain(|id, _| id != &args.target_id)` permanently removes the target's exit-code record.

**Why the second waiter gets `WAIT_FAILURE` — line 578:** [2](#0-1) 

When a non-root VM terminates, it is removed from `states` at line 402: [3](#0-2) 

After the first waiter consumes the `terminated_vms` entry, the target VM is present in **neither** `terminated_vms` nor `states`. The second waiter falls through to the `!states.contains_key` branch and receives `WAIT_FAILURE`.

**Concrete reachable sequence:**

1. Root spawns C (exits immediately with code 42), A, and B — all `Runnable`.
2. C runs and terminates → `terminated_vms[C] = 42`, C removed from `states`. No VM is yet in `VmState::Wait`, so the joining-VM loop (lines 375–398) notifies nobody.
3. A runs, calls `ckb_wait(C)` → yields → `process_message_box` finds C in `terminated_vms` → A gets `SUCCESS` + code 42 → **C evicted from `terminated_vms`**.
4. B runs, calls `ckb_wait(C)` → yields → `process_message_box` finds C in neither map → B gets `WAIT_FAILURE`.

The `Wait` syscall always yields immediately after pushing one message: [4](#0-3) 

`process_message_box` is called after every single VM yield: [5](#0-4) 

Two `Wait` messages therefore can never be in the box simultaneously; the bug spans two separate iterations, not one.

## Impact Explanation

This is an incorrect implementation of CKB-VM system call semantics. A script author who legitimately spawns one child and has two sibling VMs both wait on it will observe the second waiter receiving `WAIT_FAILURE` regardless of the child's actual exit code. This causes incorrect on-chain script validation — a valid script fails, or a script designed to branch on the error code behaves incorrectly. This matches the allowed High impact: **"Incorrect implementation or behavior of CKB-VM or system scripts"** (10001–15000 points).

## Likelihood Explanation

Any unprivileged script author can trigger this by submitting a transaction whose lock/type script spawns one child VM and has two or more sibling VMs call `ckb_wait` on it. No special keys, privileges, or network majority are required. The execution order (child terminates before either waiter calls `ckb_wait`) is fully controlled by the script author through spawn ordering and immediate-exit logic. The bug is deterministic and 100% reproducible.

## Recommendation

Remove the destructive `retain` call at line 575. The `terminated_vms` entry for a VM should not be evicted after the first consumer. Instead, retain the entry for the lifetime of the scheduler (or until all VMs that could reference the target have themselves terminated). If bounded memory is a concern, track a reference count of potential waiters per spawned VM and only evict when the count reaches zero.

## Proof of Concept

Write a scheduler integration test:

1. Root VM spawns C (exits with code 42), then spawns A and B.
2. C is scheduled first and terminates.
3. A is scheduled, calls `ckb_wait(C_id)` — assert return value is `SUCCESS` and exit code is 42.
4. B is scheduled, calls `ckb_wait(C_id)` — assert return value is `SUCCESS` and exit code is 42.

Under the current code, step 4 fails: B receives `WAIT_FAILURE` because `terminated_vms` no longer contains C after step 3. Removing the `retain` call at line 575 makes both assertions pass.

### Citations

**File:** script/src/scheduler.rs (L357-359)
```rust
        // Process message box, update VM states accordingly
        self.process_message_box()?;
        assert!(self.message_box.lock().expect("lock").is_empty());
```

**File:** script/src/scheduler.rs (L400-404)
```rust
                    self.fds.retain(|_, vm_id| *vm_id != vm_id_to_run);
                    // Clear terminated VM states
                    self.states.remove(&vm_id_to_run);
                    self.instantiated.remove(&vm_id_to_run);
                    self.suspended.remove(&vm_id_to_run);
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

**File:** script/src/syscalls/wait.rs (L39-51)
```rust
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
