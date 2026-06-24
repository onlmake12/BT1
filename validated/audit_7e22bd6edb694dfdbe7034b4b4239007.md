Audit Report

## Title
Self-Wait Deadlock in `process_message_box` Returns `Error::Unexpected` Instead of `WAIT_FAILURE` — (`script/src/scheduler.rs`)

## Summary
A script can call `ckb_wait` with `A0` set to its own `VmId`. The `process_message_box` handler in `scheduler.rs` has no self-wait guard, so it transitions the only runnable VM into `VmState::Wait { target_vm_id: self }`. On the next iteration, `iterate_prepare_machine` finds no runnable VM and returns `Error::Unexpected("A deadlock situation has been reached!")` — an internal error that should never be reachable via script-controlled input. The correct behavior is to return `WAIT_FAILURE` (error code 5).

## Finding Description

**Step 1 — Attacker entrypoint:** A script sets `A7=WAIT`, `A0=own_vm_id`, `A1=valid_addr` and executes the `ckb_wait` syscall. This is a standard, unprivileged execution path.

**Step 2 — `Wait::ecall`** (`script/src/syscalls/wait.rs`): `target_id` is read directly from `A0` with no validation. A `Message::Wait(self.id, WaitArgs { target_id: self.id, ... })` is pushed to the message box, then `Err(VMError::Yield)` is returned. [1](#0-0) 

**Step 3 — `process_message_box`** (`script/src/scheduler.rs`): The handler has two guards before inserting `VmState::Wait`:

- **Guard 1** (line 565): Is `args.target_id` in `terminated_vms`? → **No** — the VM just yielded, it is not terminated. Guard passes.
- **Guard 2** (line 578): Is `args.target_id` absent from `self.states`? → **No** — the calling VM is still present in `self.states` as `VmState::Runnable` at this point (state is only updated after `process_message_box` returns). Guard passes.

Both guards pass. The handler falls through to the unconditional insert where `vm_id == args.target_id`: [2](#0-1) 

The VM is now waiting for itself.

**Step 4 — `iterate_process_results`** (line 408): `Err(Error::Yield)` is silently swallowed as `Ok(())`. No error is surfaced yet. [3](#0-2) 

**Step 5 — Next call to `iterate_prepare_machine`** (lines 335–346): The scheduler scans `self.states` for any `VmState::Runnable` entry. The only VM is now in `VmState::Wait`. No runnable VM is found, so `Error::Unexpected("A deadlock situation has been reached!")` is returned. [4](#0-3) 

This propagates up through `iterate_inner` → `iterate_outer` → `run`, surfacing as an internal unexpected error from script verification instead of the expected `WAIT_FAILURE`.

## Impact Explanation

This is a confirmed instance of **incorrect implementation or behavior of CKB-VM** (High, 10001–15000 points). The invariant violated is: script-controlled inputs must never escalate to `Error::Unexpected`. `Error::Unexpected` is reserved for internal logic errors (corrupted state, programming bugs). Any upstream code that distinguishes `Error::Unexpected` from normal script failure — for logging, error reporting, or error-code-based branching — will misclassify this as an internal node fault rather than a script-level rejection. The error semantics are definitively wrong and the CKB-VM behavior is incorrect.

## Likelihood Explanation

Any script author can trigger this with a single instruction sequence: set `A7=WAIT`, `A0=own_pid`, `A1=valid_writable_addr`. No special privileges, no PoW, no key material, no majority hashpower required. The attack surface is the standard transaction submission path and is trivially repeatable.

## Recommendation

Add a self-wait guard in `process_message_box` before the `VmState::Wait` insert:

```rust
// In the Message::Wait branch, after the two existing guards:
if args.target_id == vm_id {
    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
    machine.inner_mut().set_register(A0, Self::u8_to_reg(WAIT_FAILURE));
    continue;
}
``` [5](#0-4) 

## Proof of Concept

1. Create a `ScriptVersion::V2` scheduler with a single root VM.
2. Set `A7=WAIT`, `A0=ROOT_VM_ID` (own ID), `A1=valid_writable_addr`.
3. Run the scheduler.
4. Assert the result is `WAIT_FAILURE` (error code 5).

Under the current code, step 4 fails — the scheduler returns `Error::Unexpected("A deadlock situation has been reached!")` instead of `WAIT_FAILURE`.

### Citations

**File:** script/src/syscalls/wait.rs (L37-50)
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
        Err(VMError::Yield)
```

**File:** script/src/scheduler.rs (L335-346)
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
```

**File:** script/src/scheduler.rs (L407-409)
```rust
            }
            Err(Error::Yield) => Ok(()),
            Err(e) => Err(e),
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
