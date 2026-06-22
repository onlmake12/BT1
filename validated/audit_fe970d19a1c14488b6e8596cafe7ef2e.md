### Title
`Scheduler::resume()` Discards Non-Zero `iteration_cycles` From Suspended State, Causing Cycle-Accounting Undercount — (File: `script/src/scheduler.rs`)

---

### Summary

`Scheduler::resume()` unconditionally resets `iteration_cycles` to `0` after restoring all other scheduler fields from a `FullSuspendedState`. However, `iteration_cycles` can be non-zero at suspension time — specifically when `process_io()` charges IO-operation cycles into `iteration_cycles` after the per-iteration reset but before the `Pause` error propagates. Those cycles are not yet reflected in `total_cycles`. Discarding them on resume permanently understates the total consumed cycles, allowing a script to execute beyond the enforced cycle limit across a suspend/resume boundary.

---

### Finding Description

The `Scheduler` tracks consumed cycles across three accumulators whose sum equals total consumed cycles at any instant:

- `total_cycles` — committed cycles from completed iterations
- `iteration_cycles` — cycles accumulated in the current iteration, not yet committed
- `machine.cycles()` — cycles inside the currently running VM [1](#0-0) 

`iterate_outer` is the only place that commits `iteration_cycles` into `total_cycles` and resets it to zero. Crucially, `process_io()` is called **after** the reset, so any cycles it charges land back in `iteration_cycles` before the function returns: [2](#0-1) 

When the scheduler is suspended (on `CyclesExceeded` or `Pause`), `suspend()` faithfully saves the non-zero `iteration_cycles` into `FullSuspendedState`: [3](#0-2) 

`FullSuspendedState` documents this explicitly: [4](#0-3) 

`resume()` initially copies `iteration_cycles` from the saved state, but then unconditionally zeroes it out: [5](#0-4) 

The comment at line 239–241 explains the intent: cycles consumed by `ensure_vms_instantiated` during resume should not be charged. However, the reset also silently discards the pre-suspension `iteration_cycles` that **do** represent real script execution work (IO-operation charges from `process_io()`). Those cycles are not in `total_cycles`, so they vanish entirely.

---

### Impact Explanation

After resume, `total_cycles` is understated by the amount of `iteration_cycles` that was discarded. The cycle-limit enforcement in `iterate_outer` is:

```
remaining_cycles = limit_cycles - iteration_cycles
``` [6](#0-5) 

Because the lost cycles are absent from both `total_cycles` and `iteration_cycles`, the scheduler believes it has more budget remaining than it actually does. A script can therefore execute additional instructions beyond the enforced limit across each suspend/resume boundary. The bypass magnitude per boundary equals the IO-operation cycles charged by `process_io()` in the final iteration before suspension. This can be compounded across repeated suspend/resume cycles (chunked verification in the tx-pool calls `resume_from_state` in a loop). [7](#0-6) 

Transactions that should be rejected for exceeding the cycle limit can be accepted into the tx-pool. If a miner includes such a transaction, consensus-layer non-resumable verification will reject the block, wasting mining effort and potentially causing chain-tip instability.

---

### Likelihood Explanation

The tx-pool uses resumable/chunked verification for all long-running scripts. Any transaction submitter can craft a script that:

1. Uses `spawn`/`pipe` syscalls to ensure pending IO operations are in flight.
2. Runs until the tx-pool's `ChunkCommand::Suspend` signal triggers a `Pause` error.
3. At that moment `process_io()` charges IO cycles into `iteration_cycles` (non-zero).
4. On the next chunk, `resume()` discards those cycles.

No privileged access, key material, or majority hashpower is required. The attacker only needs to submit a transaction.

---

### Recommendation

In `resume()`, preserve the `iteration_cycles` value from the suspended state **after** `ensure_vms_instantiated` completes, rather than zeroing it. Only the cycles added by `ensure_vms_instantiated` itself should be discarded. A minimal fix:

```rust
let saved_iteration_cycles = full.iteration_cycles;
// ... build scheduler with iteration_cycles: full.iteration_cycles ...
scheduler.ensure_vms_instantiated(&full.instantiated_ids).unwrap();
// Discard only the cycles charged by ensure_vms_instantiated (resume overhead),
// but restore the pre-suspension iteration_cycles that are not yet in total_cycles.
scheduler.iteration_cycles = saved_iteration_cycles;
```

Alternatively, `suspend()` should flush `iteration_cycles` into `total_cycles` before saving state, so that `iteration_cycles` is always zero at suspension boundaries and the reset in `resume()` is safe.

---

### Proof of Concept

**Trace showing cycles lost:**

1. `iterate_outer` runs: VM executes 800 cycles → `iteration_cycles = 800`.
2. `consume_cycles(800)` → `total_cycles = 800`. `iteration_cycles = 0`.
3. `process_io()` charges 50 IO cycles → `iteration_cycles = 50`.
4. `iterate_return?` returns `Err(Pause)` → `iterate_outer` propagates the error.
5. `chunk_run` catches `Pause`, calls `scheduler.suspend()`.
6. `suspend()` saves: `total_cycles = 800`, `iteration_cycles = 50`.
   — Note: the 50 IO cycles are **not** in `total_cycles`.
7. `resume()` restores `total_cycles = 800`, then sets `iteration_cycles = 0`.
   — The 50 cycles are permanently lost.
8. Next chunk runs with `limit_cycles = 1000` (full chunk budget).
   — Scheduler believes only 800 cycles have been consumed; actual is 850.
   — Script gets 50 extra cycles of execution budget per suspend/resume cycle.

Repeated across N suspend/resume cycles: `N × (IO cycles per iteration)` of bypass accumulates. [8](#0-7) [2](#0-1) [9](#0-8)

### Citations

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

**File:** script/src/scheduler.rs (L247-276)
```rust
    pub fn suspend(mut self) -> Result<FullSuspendedState, Error> {
        assert!(self.message_box.lock().expect("lock").is_empty());
        let mut vms = Vec::with_capacity(self.states.len());
        let instantiated_ids: Vec<_> = self.instantiated.keys().cloned().collect();
        for id in &instantiated_ids {
            self.suspend_vm(id)?;
        }
        for (id, state) in self.states {
            let snapshot = self
                .suspended
                .remove(&id)
                .ok_or_else(|| Error::Unexpected("Unable to find VM Id".to_string()))?;
            vms.push((id, state, snapshot));
        }
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
    }
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

**File:** script/src/types.rs (L498-501)
```rust
    /// Iteration cycles. Due to an implementation bug in Meepo hardfork,
    /// this value will not always be zero at visible execution boundaries.
    /// We will have to preserve this value.
    pub iteration_cycles: Cycle,
```

**File:** script/src/verify.rs (L276-341)
```rust
    pub fn resume_from_state(
        &self,
        state: &TransactionState,
        limit_cycles: Cycle,
    ) -> Result<VerifyResult, Error> {
        let TransactionState {
            current,
            state,
            current_cycles,
            ..
        } = state;

        let mut current_used = 0;
        let mut cycles = *current_cycles;

        let (_hash, current_group) = self.groups().nth(*current).ok_or_else(|| {
            ScriptError::Other(format!("snapshot group missing {current:?}")).unknown_source()
        })?;

        let resumed_script_result =
            self.verify_group_with_chunk(current_group, limit_cycles, state);

        match resumed_script_result {
            Ok(ChunkState::Completed(used_cycles, consumed_cycles)) => {
                current_used = wrapping_cycles_add(current_used, consumed_cycles, current_group)?;
                cycles = wrapping_cycles_add(cycles, used_cycles, current_group)?;
            }
            Ok(ChunkState::Suspended(state)) => {
                let state = TransactionState::new(state, *current, cycles, limit_cycles);
                return Ok(VerifyResult::Suspended(state));
            }
            Err(e) => {
                #[cfg(feature = "logging")]
                logging::on_script_error(_hash, &self.hash(), &e);
                return Err(e.source(current_group).into());
            }
        }

        for (idx, (_hash, group)) in self.groups().enumerate().skip(current + 1) {
            let remain_cycles = limit_cycles.checked_sub(current_used).ok_or_else(|| {
                ScriptError::Other(format!(
                    "expect invalid cycles {limit_cycles} {current_used} {cycles}"
                ))
                .source(group)
            })?;

            match self.verify_group_with_chunk(group, remain_cycles, &None) {
                Ok(ChunkState::Completed(_, consumed_cycles)) => {
                    current_used = wrapping_cycles_add(current_used, consumed_cycles, group)?;
                    cycles = wrapping_cycles_add(cycles, consumed_cycles, group)?;
                }
                Ok(ChunkState::Suspended(state)) => {
                    let current = idx;
                    let state = TransactionState::new(state, current, cycles, remain_cycles);
                    return Ok(VerifyResult::Suspended(state));
                }
                Err(e) => {
                    #[cfg(feature = "logging")]
                    logging::on_script_error(_hash, &self.hash(), &e);
                    return Err(e.source(group).into());
                }
            }
        }

        Ok(VerifyResult::Completed(cycles))
    }
```

**File:** script/src/verify.rs (L470-510)
```rust
    fn chunk_run(
        &self,
        script_group: &ScriptGroup,
        max_cycles: Cycle,
        state: &Option<FullSuspendedState>,
    ) -> Result<ChunkState, ScriptError> {
        let mut scheduler = if let Some(state) = state {
            self.resume_scheduler(script_group, state)
        } else {
            self.create_scheduler(script_group)
        }?;
        let previous_cycles = scheduler.consumed_cycles();
        let res = scheduler.run(RunMode::LimitCycles(max_cycles));
        match res {
            Ok(TerminatedResult {
                exit_code,
                consumed_cycles: cycles,
            }) => {
                if exit_code == 0 {
                    Ok(ChunkState::Completed(
                        cycles,
                        scheduler.consumed_cycles() - previous_cycles,
                    ))
                } else {
                    Err(ScriptError::validation_failure(
                        &script_group.script,
                        exit_code,
                    ))
                }
            }
            Err(error) => match error {
                VMInternalError::CyclesExceeded | VMInternalError::Pause => {
                    let snapshot = scheduler
                        .suspend()
                        .map_err(|err| self.map_vm_internal_error(err, max_cycles))?;
                    Ok(ChunkState::suspended(snapshot))
                }
                _ => Err(self.map_vm_internal_error(error, max_cycles)),
            },
        }
    }
```
