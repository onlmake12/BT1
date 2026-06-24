Audit Report

## Title
Non-Deterministic `CURRENT_CYCLES` Syscall Due to `iteration_cycles` Loss on Scheduler Resume — (`script/src/scheduler.rs`, `script/src/syscalls/current_cycles.rs`)

## Summary
The `CURRENT_CYCLES` syscall (2042) returns only `total_cycles + machine.cycles()`, permanently omitting `iteration_cycles`. After each `iterate_outer` call, `process_io()` populates `iteration_cycles` with `SPAWN_EXTRA_CYCLES_BASE` charges from internal `resume_vm()`/`suspend_vm()` calls. When the outer scheduler is suspended at this point and later resumed, `Scheduler::resume()` unconditionally zeros `iteration_cycles` at line 242, permanently discarding those charges. Nodes with different `step_cycles` configurations suspend at different iteration boundaries, losing different cumulative amounts, causing `CURRENT_CYCLES` to return divergent values for the same script execution. A V2 script that branches on this syscall can produce different exit codes on different nodes, breaking consensus.

## Finding Description

**Root cause — `CURRENT_CYCLES` omits `iteration_cycles`:**

`CurrentCycles::ecall` reads only `base_cycles` (an `Arc` clone of `total_cycles`) plus the currently-running machine's own cycle counter: [1](#0-0) 

`base_cycles` is wired directly to `total_cycles` at VM creation time: [2](#0-1) 

`iteration_cycles` is never included.

**How `iteration_cycles` becomes non-zero after `iterate_outer`:**

`iterate_outer` runs the VM, moves `iteration_cycles` to `total_cycles`, resets `iteration_cycles = 0`, then calls `process_io()`: [3](#0-2) 

Inside `process_io()`, `ensure_vms_instantiated()` is called to bring read/write VMs into memory: [4](#0-3) 

`ensure_vms_instantiated` calls `resume_vm()` and `suspend_vm()`, each of which adds `SPAWN_EXTRA_CYCLES_BASE` directly to `iteration_cycles`: [5](#0-4) [6](#0-5) 

After `process_io()` returns, `iteration_cycles` is non-zero and has NOT yet been moved to `total_cycles`.

**`suspend()` preserves the non-zero `iteration_cycles`:** [7](#0-6) 

**`resume()` unconditionally zeros it, discarding the IO-processing charges:** [8](#0-7) 

The comment justifies this as "not charging cycles for suspend/resume overhead," but it also discards the `SPAWN_EXTRA_CYCLES_BASE` charges that were accumulated by `process_io()` in the *previous* iteration — charges that are not outer-scheduler overhead and that would have been moved to `total_cycles` at the next `consume_cycles()` call in the non-suspended path.

**The divergence:**

- **Node A** (large `step_cycles`, no suspension between iteration N and N+1): `SPAWN_EXTRA_CYCLES_BASE` charges from `process_io()` at iteration N flow into `total_cycles` at iteration N+1's `consume_cycles()`. `CURRENT_CYCLES` in iteration N+1 reflects them.
- **Node B** (small `step_cycles`, suspends after iteration N): those same charges are in `iteration_cycles` at suspension, zeroed at resume, and permanently absent from `total_cycles`. `CURRENT_CYCLES` in iteration N+1 returns a smaller value by exactly the lost `SPAWN_EXTRA_CYCLES_BASE` amount.

The code explicitly acknowledges this class of bug: [9](#0-8) 

The acknowledgment covers the general pattern but does not eliminate the non-determinism: the comment describes a single consistent behavior, but the actual behavior varies with `step_cycles`, which is a per-node configuration parameter, not a consensus parameter.

## Impact Explanation

A crafted V2 script that: (a) spawns a child VM connected via pipe, (b) performs a pipe read/write (triggering `ensure_vms_instantiated` inside `process_io()`, charging `SPAWN_EXTRA_CYCLES_BASE` into `iteration_cycles`), (c) calls `CURRENT_CYCLES`, and (d) exits 0 if the result exceeds a threshold calibrated to the expected discrepancy — will produce exit code 0 on nodes that did not suspend after the relevant iteration and exit code 1 on nodes that did. Two nodes producing different exit codes for the same transaction constitutes a **consensus deviation**, matching the Critical impact class: "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation

Moderate. Script deployment on CKB is permissionless, so no special privilege is required. The attacker must deploy a specifically crafted lock or type script and submit a transaction that uses it. `step_cycles` legitimately differs between nodes (it is a node-level tuning parameter), so the precondition is always satisfied on a real network. The bug is documented in source as intentionally preserved for Meepo hardfork, reducing the probability of an immediate patch, but also meaning developers are aware of the class of issue.

## Recommendation

In `Scheduler::resume()`, instead of unconditionally zeroing `iteration_cycles`, add the saved value into `total_cycles` before zeroing, so that IO-processing charges from the previous iteration are not lost:

```rust
// Add IO-processing charges that were pending at suspension time
self.consume_cycles(full.iteration_cycles).unwrap();
// Then zero, so that the outer-scheduler resume overhead (added by
// ensure_vms_instantiated below) is still not charged.
scheduler.iteration_cycles = 0;
```

Alternatively, include `iteration_cycles` in the value returned by `CURRENT_CYCLES` (making it `total_cycles + iteration_cycles + machine.cycles()`). Either fix should be scheduled for the hardfork that supersedes Meepo, with the comment at line 242 updated to distinguish outer-scheduler overhead (legitimately uncharged) from IO-processing charges (which must be preserved for determinism).

## Proof of Concept

Differential unit test using `TransactionScriptsVerifier` or direct `Scheduler` API:

1. Deploy a V2 lock script that: spawns a child VM sharing a pipe, transfers N bytes over the pipe (causing `ensure_vms_instantiated` inside `process_io()` to call `resume_vm`/`suspend_vm`, adding `SPAWN_EXTRA_CYCLES_BASE` to `iteration_cycles`), calls `CURRENT_CYCLES` (syscall 2042), exits 0 if result > `THRESHOLD`, else exits 1. Set `THRESHOLD` between the two expected cycle counts (differing by `SPAWN_EXTRA_CYCLES_BASE` per suspend/resume pair).
2. Verify the same transaction twice:
   - Run A: `step_cycles = u64::MAX` (no outer-scheduler suspension).
   - Run B: `step_cycles = 1` (suspend after every iteration).
3. Assert both runs return the same exit code — the assertion will fail, demonstrating the consensus deviation.

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

**File:** script/src/scheduler.rs (L239-242)
```rust
        // NOTE: suspending/resuming a scheduler is part of CKB's implementation
        // details. It is not part of execution consensue. We should not charge
        // cycles for them.
        scheduler.iteration_cycles = 0;
```

**File:** script/src/scheduler.rs (L261-267)
```rust
        Ok(FullSuspendedState {
            // NOTE: suspending a scheduler is actually part of CKB's
            // internal execution logic, it does not belong to VM execution
            // consensus. We are not charging cycles for suspending
            // a VM in the process of suspending the whole scheduler.
            total_cycles: self.total_cycles.load(Ordering::Acquire),
            iteration_cycles: self.iteration_cycles,
```

**File:** script/src/scheduler.rs (L422-453)
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
```

**File:** script/src/scheduler.rs (L802-802)
```rust
            self.ensure_vms_instantiated(&[read_vm_id, write_vm_id])?;
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

**File:** script/src/scheduler.rs (L1086-1088)
```rust
        let vm_context = VmContext {
            base_cycles: Arc::clone(&self.total_cycles),
            message_box: Arc::clone(&self.message_box),
```
