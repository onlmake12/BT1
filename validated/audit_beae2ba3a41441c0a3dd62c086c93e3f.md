Audit Report

## Title
Non-Deterministic `CURRENT_CYCLES` Syscall Due to `iteration_cycles` Loss on Scheduler Resume — (`script/src/scheduler.rs`, `script/src/syscalls/current_cycles.rs`)

## Summary
`CURRENT_CYCLES` (syscall 2042) computes its return value as `total_cycles + machine.cycles()`, permanently excluding `iteration_cycles`. After `iterate_outer` zeros `iteration_cycles` and calls `process_io()`, internal `resume_vm`/`suspend_vm` calls re-populate `iteration_cycles` with `SPAWN_EXTRA_CYCLES_BASE` charges. When `Scheduler::resume()` is called, line 242 unconditionally zeros `iteration_cycles`, permanently discarding those VM-operation charges from `total_cycles`. Because `step_cycles` is a node-level (non-consensus) parameter, nodes suspending at different boundaries lose different amounts of cycles, causing `CURRENT_CYCLES` to return different values for the same script execution — a consensus split.

## Finding Description

**`CURRENT_CYCLES` omits `iteration_cycles`:**
`CurrentCycles::ecall` reads only `self.base.load(Ordering::Acquire).checked_add(machine.cycles())`, where `self.base` is `Arc::clone(&vm_context.base_cycles)` — the same `Arc<AtomicU64>` as the scheduler's `total_cycles`. [1](#0-0) 
`iteration_cycles` is never included in this computation.

**`iterate_outer` leaves `iteration_cycles` non-zero after `process_io()`:**
The sequence in `iterate_outer` is: (1) run VM via `iterate_inner`, (2) `consume_cycles(self.iteration_cycles)` moves charges to `total_cycles`, (3) `self.iteration_cycles = 0`, (4) `self.process_io()`. [2](#0-1) 
Inside `process_io()`, `ensure_vms_instantiated` is called at line 802, which may invoke `resume_vm` and/or `suspend_vm`. [3](#0-2) 
Each `resume_vm` call adds `SPAWN_EXTRA_CYCLES_BASE` directly to `iteration_cycles`: [4](#0-3) 
Each `suspend_vm` call does the same: [5](#0-4) 
After `process_io()` returns, `iteration_cycles` is non-zero and has not been moved to `total_cycles`.

**`FullSuspendedState` captures the non-zero `iteration_cycles`:**
`Scheduler::suspend()` saves `iteration_cycles: self.iteration_cycles` into `FullSuspendedState`, preserving whatever non-zero value was left by `process_io()`. [6](#0-5) 

**`Scheduler::resume()` unconditionally zeros `iteration_cycles`:**
After restoring `total_cycles` from `full.total_cycles`, line 242 sets `scheduler.iteration_cycles = 0`, permanently discarding the `SPAWN_EXTRA_CYCLES_BASE` cycles that were in `iteration_cycles` at suspension time. Those cycles never reach `total_cycles`. [7](#0-6) 
The comment justifies this as "scheduler suspend/resume is an implementation detail," but the cycles being discarded are from VM-level `resume_vm`/`suspend_vm` operations inside `process_io()` — not from the scheduler-level suspend/resume itself. The comment's justification does not apply to the cycles actually being dropped.

**The code acknowledges this class of bug explicitly:** [8](#0-7) 
However, "freezing" the bug for Meepo hardfork compatibility does not eliminate the consensus risk. The frozen behavior is only reproducible when `step_cycles` is identical across nodes. Since `step_cycles` is a node-level performance parameter (not a consensus rule), two nodes running identical code with different `step_cycles` values will suspend the scheduler at different boundaries, losing different amounts of `SPAWN_EXTRA_CYCLES_BASE` cycles, and thus returning different values from `CURRENT_CYCLES` for the same script execution.

**Existing guards are insufficient:**
The `ensure_vms_instantiated` call inside `Scheduler::resume()` at line 237 itself adds `SPAWN_EXTRA_CYCLES_BASE` to `iteration_cycles` via `resume_vm`, and then line 242 immediately zeros it — so even the resume-time VM instantiation cycles are silently dropped. [9](#0-8) 

## Impact Explanation
A permissionless attacker deploys a V2 lock or type script that: (a) spawns child VMs connected via pipe to force `process_io()` to call `ensure_vms_instantiated` with VM swapping, charging `SPAWN_EXTRA_CYCLES_BASE` into `iteration_cycles`; (b) calls `CURRENT_CYCLES` (syscall 2042); (c) branches on whether the result exceeds a threshold calibrated to straddle the per-suspension `SPAWN_EXTRA_CYCLES_BASE` discrepancy. Nodes with different `step_cycles` produce different exit codes for the same transaction. This is a **consensus deviation** — Critical impact (15001–25000 points) under the CKB bounty scope.

## Likelihood Explanation
Moderate-to-high. Script deployment on CKB is fully permissionless. The attacker needs only to publish a transaction using the crafted script. The `step_cycles` parameter legitimately differs across node operators as a performance tuning knob, not a consensus rule. The bug is documented in source but explicitly preserved, reducing the chance of an emergency patch. The exploit requires no victim mistakes, no leaked keys, and no privileged access.

## Recommendation
Two viable fixes:

**(a) Include `iteration_cycles` in `CURRENT_CYCLES`:** Pass `iteration_cycles` into `VmContext` so `CurrentCycles::ecall` can read `total_cycles + iteration_cycles + machine.cycles()`. This requires atomic or synchronized access to `iteration_cycles`.

**(b) Preserve `iteration_cycles` through scheduler resume:** In `Scheduler::resume()`, instead of zeroing `iteration_cycles` at line 242, add the saved `full.iteration_cycles` into `total_cycles` before zeroing, so no charged VM-operation cycles are silently dropped. The comment should distinguish between scheduler-level suspend/resume overhead (legitimately unchargeable) and IO-transfer/VM-swap cycles (which must be preserved for determinism).

Either fix should be scheduled for the next hardfork superseding Meepo, with a note that the change alters `CURRENT_CYCLES` observable values and is therefore consensus-breaking.

## Proof of Concept
Differential unit test:

1. Write a V2 lock script (RISC-V binary) that: spawns a child VM, creates a pipe, performs a write from child and read from parent (forcing `process_io()` to call `ensure_vms_instantiated` with VM swapping, charging `SPAWN_EXTRA_CYCLES_BASE` into `iteration_cycles`), then calls `CURRENT_CYCLES` (syscall 2042), exits 0 if result ≥ `THRESHOLD`, exits 1 otherwise. Set `THRESHOLD` between the cycle count observed with zero scheduler suspensions and the count observed with one suspension.

2. In a Rust test, construct a `TransactionScriptsVerifier` and verify the same transaction twice:
   - Run A: `step_cycles = u64::MAX` (single pass, no scheduler suspension → `iteration_cycles` never zeroed mid-execution).
   - Run B: `step_cycles = 1` (suspend after every cycle → `iteration_cycles` zeroed on each resume, losing `SPAWN_EXTRA_CYCLES_BASE` per suspension).

3. Assert both runs return the same exit code. The assertion will fail, demonstrating the consensus deviation.

### Citations

**File:** script/src/syscalls/current_cycles.rs (L37-41)
```rust
        let cycles = self
            .base
            .load(Ordering::Acquire)
            .checked_add(machine.cycles())
            .ok_or(VMError::CyclesOverflow)?;
```

**File:** script/src/scheduler.rs (L88-94)
```rust
    /// NOTE: the above workflow describes the optimal case: `iteration_cycles`
    /// will always be zero after each iteration. However, our initial implementation
    /// for Meepo hardfork contains a bug: cycles charged by suspending / resuming
    /// VMs when processing IOs, will not be reflected in `current cycles` syscalls
    /// of the subsequent running VMs. To preserve this behavior, consumed cycles in
    /// iteration_cycles cannot be moved at iterate boundaries. Later hardfork versions
    /// might fix this, but for the Meepo hardfork, we will have to preserve this behavior.
```

**File:** script/src/scheduler.rs (L236-243)
```rust
        scheduler
            .ensure_vms_instantiated(&full.instantiated_ids)
            .unwrap();
        // NOTE: suspending/resuming a scheduler is part of CKB's implementation
        // details. It is not part of execution consensue. We should not charge
        // cycles for them.
        scheduler.iteration_cycles = 0;
        scheduler
```

**File:** script/src/scheduler.rs (L261-268)
```rust
        Ok(FullSuspendedState {
            // NOTE: suspending a scheduler is actually part of CKB's
            // internal execution logic, it does not belong to VM execution
            // consensus. We are not charging cycles for suspending
            // a VM in the process of suspending the whole scheduler.
            total_cycles: self.total_cycles.load(Ordering::Acquire),
            iteration_cycles: self.iteration_cycles,
            next_vm_id: self.next_vm_id,
```

**File:** script/src/scheduler.rs (L422-455)
```rust
        let iterate_return = self.iterate_inner(pause.clone(), limit_cycles);
        self.consume_cycles(self.iteration_cycles)?;
        let remaining_cycles = limit_cycles
            .checked_sub(self.iteration_cycles)
            .ok_or(Error::CyclesExceeded)?;
        // Clear iteration cycles intentionally after each run
        self.iteration_cycles = 0;
        // Process all pending VM reads & writes. Notice ideally, this invocation
        // should be put at the end of `iterate_inner` function. However, 2 things
        // prevent this:
        //
        // * In earlier implementation of the Meepo hardfork version, `self.process_io`
        // was put at the very start of +iterate_prepare_machine+ method. Meaning we used
        // to process IO syscalls at the very start of a new iteration.
        // * Earlier implementation contains a bug that cycles consumed by suspending / resuming
        // VMs are not updated in the subsequent VM's `current cycles` syscalls.
        //
        // To make ckb-script package suitable for outside usage, we want IOs processed at
        // the end of each iteration, not at the start of the next iteration. We also need
        // to replicate the exact same runtime behavior of Meepo hardfork. This means the only
        // viable change will be:
        //
        // * Move `self.process_io` call to the very end of `iterate_outer` method, which is
        // exactly current location
        // * For now we have to live with the fact that `iteration_cycles` will not always be
        // zero at iteration boundaries, and also preserve its value in `FullSuspendedState`.
        //
        // One expected change is that +process_io+ is now called once more
        // after the whole scheduler terminates, and not called at the very beginning
        // when no VM is executing. But since no VMs will be in IO states at this 2 timeslot,
        // we should be fine here.
        self.process_io()?;
        let id = iterate_return?;
        Ok((id, remaining_cycles))
```

**File:** script/src/scheduler.rs (L800-803)
```rust
            } = write_state;

            self.ensure_vms_instantiated(&[read_vm_id, write_vm_id])?;
            {
```

**File:** script/src/scheduler.rs (L954-957)
```rust
        self.iteration_cycles = self
            .iteration_cycles
            .checked_add(SPAWN_EXTRA_CYCLES_BASE)
            .ok_or(Error::CyclesExceeded)?;
```

**File:** script/src/scheduler.rs (L976-979)
```rust
        self.iteration_cycles = self
            .iteration_cycles
            .checked_add(SPAWN_EXTRA_CYCLES_BASE)
            .ok_or(Error::CyclesExceeded)?;
```
