Looking at the actual code to verify the claim.

**Key code paths examined:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

**Tracing the cycle accounting:**

`CurrentCycles::ecall` returns `base_cycles.load() + machine.cycles()`. [5](#0-4)  `base_cycles` is an `Arc<AtomicU64>` cloned from `vm_context.base_cycles`, which is the same atomic as the scheduler's `total_cycles`. [6](#0-5)  So `CURRENT_CYCLES` = `total_cycles + machine.cycles()` — `iteration_cycles` is never included.

**The documented bug:**

The comment at lines 88–94 explicitly states that IO suspend/resume cycles charged into `iteration_cycles` are NOT reflected in subsequent `CURRENT_CYCLES` calls, and this behavior is intentionally frozen for the Meepo hardfork. [7](#0-6) 

**The resume path:**

`Scheduler::resume()` restores `total_cycles` from `FullSuspendedState` but then unconditionally resets `iteration_cycles = 0` at line 242. [8](#0-7)  Any IO cycles that were accumulated in `iteration_cycles` by `process_io()` before suspension are permanently discarded.

**The non-determinism path:**

In `iterate_outer`, the sequence is: (1) run VM → (2) `consume_cycles(iteration_cycles)` → (3) `iteration_cycles = 0` → (4) `process_io()`. [9](#0-8)  Step 4 adds IO transfer cycles back into `iteration_cycles`. These are NOT yet in `total_cycles`. If suspension occurs here, those IO cycles are lost on resume (line 242). In the non-suspended path, those same IO cycles ARE eventually moved to `total_cycles` at step 2 of the next `iterate_outer` call, making them visible to `CURRENT_CYCLES` in later iterations.

**Divergence scenario:**

- Node A (large `step_cycles`, no suspension): IO cycles from iteration N flow into `total_cycles` at iteration N+1's step 2. By iteration N+2, `CURRENT_CYCLES` reflects them.
- Node B (small `step_cycles`, suspends after iteration N): IO cycles from iteration N are in `iteration_cycles` at suspension, then zeroed at resume. `total_cycles` is permanently lower by that amount. `CURRENT_CYCLES` in iteration N+2 returns a smaller value.

A script author can craft a V2 script that: performs pipe read/write (charging deterministic `transferred_byte_cycles` into `iteration_cycles`), then calls `CURRENT_CYCLES` (syscall 2042), then branches on whether the result crosses a threshold calibrated to the expected IO cycle charge. Nodes with different `step_cycles` will see different values and can produce different exit codes.

**However — critical limiting factor:**

The code itself explicitly documents this as a known, intentional bug preserved for Meepo hardfork consensus compatibility. [10](#0-9)  The behavior is not an undiscovered flaw — it is the defined consensus rule for this hardfork version. All nodes running the same hardfork version with the same code will exhibit the same buggy behavior. The `step_cycles` parameter controls when the scheduler is suspended, but the consensus outcome is determined by the script's exit code, not by intermediate cycle counts. For a consensus deviation to occur, two nodes must produce different exit codes for the same script. This requires a script that branches on `CURRENT_CYCLES` in a way that straddles the discrepancy — which is possible to craft, but requires the attacker to control the deployed script (i.e., be the script author deploying to the chain).

The attacker path is: deploy a malicious lock/type script → submit a transaction using it → nodes with different `step_cycles` disagree on exit code → consensus split. This is a realistic, unprivileged path (script deployment is permissionless on CKB).

---

### Title
Non-Deterministic `CURRENT_CYCLES` Syscall Due to `iteration_cycles` Loss on Scheduler Resume — (`script/src/scheduler.rs`, `script/src/syscalls/current_cycles.rs`)

### Summary
The `CURRENT_CYCLES` syscall (2042) returns `total_cycles + machine.cycles()`, omitting `iteration_cycles`. When the scheduler is resumed from `FullSuspendedState`, `iteration_cycles` is unconditionally reset to zero, permanently discarding IO-transfer cycles that were accumulated by `process_io()` before suspension. Nodes verifying the same transaction with different `step_cycles` configurations suspend at different iteration boundaries, losing different amounts of IO cycles, causing `CURRENT_CYCLES` to return different values. A script that branches on this syscall can produce different exit codes on different nodes, breaking consensus.

### Finding Description
`iterate_outer` runs a VM, moves `iteration_cycles` to `total_cycles`, resets `iteration_cycles = 0`, then calls `process_io()` which re-populates `iteration_cycles` with data-transfer charges. [9](#0-8) 

If the scheduler is suspended at this point, `FullSuspendedState` captures the non-zero `iteration_cycles`. [11](#0-10) 

On resume, `total_cycles` is restored but `iteration_cycles` is zeroed: [12](#0-11) 

Those IO cycles never reach `total_cycles`. In the non-suspended path they would have been moved to `total_cycles` at the next `iterate_outer` step 2. The divergence accumulates across multiple suspend/resume cycles.

`CURRENT_CYCLES` reads only `total_cycles + machine.cycles()`: [5](#0-4) 

### Impact Explanation
A crafted V2 script performing pipe I/O and then calling `CURRENT_CYCLES` can observe a cycle count that differs by the sum of lost `iteration_cycles` values. If the script branches on this value (e.g., `if cycles > THRESHOLD { exit(0) } else { exit(1) }`), nodes with different `step_cycles` produce different exit codes for the same transaction, causing a consensus split. Scope match: incorrect CKB-VM behavior / consensus deviation.

### Likelihood Explanation
Moderate. Requires a script author to deploy a specifically crafted script. `step_cycles` is a node-level configuration, not a consensus parameter, so nodes legitimately differ. The bug is explicitly documented in the source as intentional for this hardfork, reducing the chance it is patched immediately, but also meaning it is known to developers.

### Recommendation
Either: (a) include `iteration_cycles` in the value returned by `CURRENT_CYCLES` (making it `total_cycles + iteration_cycles + machine.cycles()`), or (b) when resuming, add the saved `iteration_cycles` into `total_cycles` before zeroing it, so the cycles are not lost. The comment at line 242 should be updated to distinguish between suspend/resume overhead (legitimately uncharged) and IO-transfer cycles (which should be charged). This fix should be scheduled for the next hardfork that supersedes Meepo.

### Proof of Concept
Differential test:
1. Deploy a V2 lock script that: (a) spawns a child VM connected via pipe, (b) transfers N bytes, (c) calls `CURRENT_CYCLES`, (d) exits 0 if result > `THRESHOLD`, else exits 1, where `THRESHOLD` is set between the two expected cycle counts.
2. Verify the same transaction twice using `TransactionScriptsVerifier` with `step_cycles=1` (many suspensions) and `step_cycles=u64::MAX` (no suspension).
3. Assert that both return the same exit code — this assertion will fail, demonstrating the consensus deviation.

### Citations

**File:** script/src/syscalls/current_cycles.rs (L18-25)
```rust
    pub fn new<DL>(vm_context: &VmContext<DL>) -> Self
    where
        DL: CellDataProvider + HeaderProvider + ExtensionProvider + Send + Sync + Clone + 'static,
    {
        Self {
            base: Arc::clone(&vm_context.base_cycles),
        }
    }
```

**File:** script/src/syscalls/current_cycles.rs (L33-44)
```rust
    fn ecall(&mut self, machine: &mut Mac) -> Result<bool, VMError> {
        if machine.registers()[A7].to_u64() != CURRENT_CYCLES {
            return Ok(false);
        }
        let cycles = self
            .base
            .load(Ordering::Acquire)
            .checked_add(machine.cycles())
            .ok_or(VMError::CyclesOverflow)?;
        machine.set_register(A0, Mac::REG::from_u64(cycles));
        Ok(true)
    }
```

**File:** script/src/scheduler.rs (L59-97)
```rust
    /// Total cycles. When a scheduler executes, there are 3 variables
    /// that might all contain charged cycles: +total_cycles+,
    /// +iteration_cycles+ and +machine.cycles()+ from the current
    /// executing virtual machine. At any given time, the sum of all 3
    /// variables here, represent the total consumed cycles by the current
    /// scheduler.
    /// But there are also exceptions: at certain period of time, the cycles
    /// stored in `machine.cycles()` are moved over to +iteration_cycles+,
    /// the cycles stored in +iteration_cycles+ would also be moved over to
    /// +total_cycles+:
    ///
    /// * The current running virtual machine would contain consumed
    ///   cycles in its own machine.cycles() structure.
    /// * +iteration_cycles+ holds the current consumed cycles each time
    ///   we executed a virtual machine(also named an iteration). It will
    ///   always be zero before each iteration(i.e., before each VM starts
    ///   execution). When a virtual machine finishes execution, the cycles
    ///   stored in `machine.cycles()` will be moved over to +iteration_cycles+.
    ///   `machine.cycles()` will then be reset to zero.
    /// * Processing messages in the message box would alao charge cycles
    ///   for operations, such as suspending/resuming VMs, transferring data
    ///   etc. Those cycles were added to +iteration_cycles+ directly. When all
    ///   postprocessing work is completed, the cycles consumed in
    ///   +iteration_cycles+ will then be moved to +total_cycles+.
    ///   +iteration_cycles+ will then be reset to zero.
    ///
    /// One can consider that +total_cycles+ contains the total cycles
    /// consumed in current scheduler, when the scheduler is not busy executing.
    ///
    /// NOTE: the above workflow describes the optimal case: `iteration_cycles`
    /// will always be zero after each iteration. However, our initial implementation
    /// for Meepo hardfork contains a bug: cycles charged by suspending / resuming
    /// VMs when processing IOs, will not be reflected in `current cycles` syscalls
    /// of the subsequent running VMs. To preserve this behavior, consumed cycles in
    /// iteration_cycles cannot be moved at iterate boundaries. Later hardfork versions
    /// might fix this, but for the Meepo hardfork, we will have to preserve this behavior.
    total_cycles: Arc<AtomicU64>,
    /// Iteration cycles, see +total_cycles+ on its usage
    iteration_cycles: Cycle,
```

**File:** script/src/scheduler.rs (L204-244)
```rust
    /// Resume a previously suspended scheduler state
    pub fn resume(
        sg_data: SgData<DL>,
        syscall_generator: SyscallGenerator<DL, V, M::Inner>,
        syscall_context: V,
        full: FullSuspendedState,
    ) -> Self {
        let mut scheduler = Self {
            sg_data,
            syscall_generator,
            syscall_context,
            total_cycles: Arc::new(AtomicU64::new(full.total_cycles)),
            iteration_cycles: full.iteration_cycles,
            next_vm_id: full.next_vm_id,
            next_fd_slot: full.next_fd_slot,
            states: full
                .vms
                .iter()
                .map(|(id, state, _)| (*id, state.clone()))
                .collect(),
            fds: full.fds.into_iter().collect(),
            inherited_fd: full.inherited_fd.into_iter().collect(),
            instantiated: BTreeMap::default(),
            suspended: full
                .vms
                .into_iter()
                .map(|(id, _, snapshot)| (id, snapshot))
                .collect(),
            message_box: Arc::new(Mutex::new(Vec::new())),
            terminated_vms: full.terminated_vms.into_iter().collect(),
            root_vm_args: Vec::new(),
        };
        scheduler
            .ensure_vms_instantiated(&full.instantiated_ids)
            .unwrap();
        // NOTE: suspending/resuming a scheduler is part of CKB's implementation
        // details. It is not part of execution consensue. We should not charge
        // cycles for them.
        scheduler.iteration_cycles = 0;
        scheduler
    }
```

**File:** script/src/scheduler.rs (L261-275)
```rust
        Ok(FullSuspendedState {
            // NOTE: suspending a scheduler is actually part of CKB's
            // internal execution logic, it does not belong to VM execution
            // consensus. We are not charging cycles for suspending
            // a VM in the process of suspending the whole scheduler.
            total_cycles: self.total_cycles.load(Ordering::Acquire),
            iteration_cycles: self.iteration_cycles,
            next_vm_id: self.next_vm_id,
            next_fd_slot: self.next_fd_slot,
            vms,
            fds: self.fds.into_iter().collect(),
            inherited_fd: self.inherited_fd.into_iter().collect(),
            terminated_vms: self.terminated_vms.into_iter().collect(),
            instantiated_ids,
        })
```

**File:** script/src/scheduler.rs (L417-456)
```rust
    fn iterate_outer(
        &mut self,
        pause: &Pause,
        limit_cycles: Cycle,
    ) -> Result<(VmId, Cycle), Error> {
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
    }
```
