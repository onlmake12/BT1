Audit Report

## Title
`Scheduler::resume()` Unconditionally Zeroes `iteration_cycles`, Discarding IO-Charged Cycles Not Yet Committed to `total_cycles` — (File: `script/src/scheduler.rs`)

## Summary

`Scheduler::resume()` restores `iteration_cycles` from `FullSuspendedState` at line 216, but then unconditionally zeroes it at line 242. At suspension time, `iteration_cycles` can be non-zero because `process_io()` charges IO-operation cycles into it *after* the per-iteration reset in `iterate_outer`, and those cycles are never committed to `total_cycles` before the `Pause` error propagates. Discarding them on resume permanently understates consumed cycles, allowing a script to execute beyond the enforced cycle limit across each suspend/resume boundary.

## Finding Description

`iterate_outer` (lines 417–456) follows this sequence:

1. Calls `iterate_inner` and stores the result in `iterate_return`.
2. Calls `consume_cycles(self.iteration_cycles)` — commits VM-execution cycles into `total_cycles`.
3. Computes `remaining_cycles = limit_cycles - self.iteration_cycles`.
4. Resets `self.iteration_cycles = 0`.
5. Calls `self.process_io()` — this can add IO-operation cycles directly into `iteration_cycles`.
6. Evaluates `let id = iterate_return?` — if `Pause`, propagates the error immediately. [1](#0-0) 

After step 4, `total_cycles` is up-to-date and `iteration_cycles` is zero. After step 5, `process_io()` may have written IO cycles back into `iteration_cycles`. When `Pause` propagates at step 6, those IO cycles sit in `iteration_cycles` and are **not** in `total_cycles`.

`suspend()` faithfully preserves this non-zero value: [2](#0-1) 

`FullSuspendedState` even documents that this value must be preserved: [3](#0-2) 

`resume()` initially copies `iteration_cycles` from the saved state (line 216), but then unconditionally zeroes it: [4](#0-3) 

The comment at lines 239–241 explains the intent is to avoid charging cycles for the resume overhead (`ensure_vms_instantiated`). However, the reset also silently discards the pre-suspension IO cycles that are not yet in `total_cycles`. `consumed_cycles()` only reads `total_cycles`: [5](#0-4) 

So those IO cycles vanish from all accounting permanently.

## Impact Explanation

After resume, `total_cycles` is understated by the IO cycles discarded. The next chunk runs with the full `limit_cycles` budget, unaware of the missing cycles. In `chunk_run`, the consumed-cycles delta is computed as `scheduler.consumed_cycles() - previous_cycles`, which only reads `total_cycles`: [6](#0-5) 

The tx-pool uses resumable/chunked verification (`resume_from_state` in a loop), so the bypass accumulates across every suspend/resume boundary. Transactions that should be rejected for exceeding the cycle limit can be accepted into the tx-pool. If a miner includes such a transaction, the consensus-layer non-resumable verifier will compute a higher cycle count and reject the block, wasting mining effort and causing chain-tip instability. This matches the allowed impact: **Incorrect implementation or behavior of CKB-VM or system scripts** (High, 10001–15000 points), with potential escalation to **Vulnerabilities which could easily damage CKB economy** (Critical) if the bypass is compounded across many boundaries with large IO transfers.

## Likelihood Explanation

No privileged access is required. Any transaction submitter can craft a script using `spawn`/`pipe` syscalls to ensure pending IO operations are in flight at suspension time. The tx-pool's chunked verification will trigger `Pause`, causing `process_io()` to charge IO cycles into `iteration_cycles` before suspension. On the next chunk, `resume()` discards those cycles. This is repeatable across every suspend/resume cycle with no additional attacker capability needed. [7](#0-6) 

## Recommendation

In `resume()`, save the pre-suspension `iteration_cycles` before calling `ensure_vms_instantiated`, then restore it afterward. Only the cycles added by `ensure_vms_instantiated` itself should be discarded:

```rust
let saved_iteration_cycles = full.iteration_cycles;
// ... build scheduler with iteration_cycles: full.iteration_cycles ...
scheduler.ensure_vms_instantiated(&full.instantiated_ids).unwrap();
// Discard only resume-overhead cycles; restore pre-suspension IO cycles.
scheduler.iteration_cycles = saved_iteration_cycles;
```

Alternatively, `suspend()` should flush `iteration_cycles` into `total_cycles` before saving state, so `iteration_cycles` is always zero at suspension boundaries and the reset in `resume()` is safe. The code comment at lines 446–447 already acknowledges this design tension. [8](#0-7) 

## Proof of Concept

1. Submit a transaction with a script that uses `spawn` + `pipe` to create pending IO between two VMs.
2. Run the script under tx-pool chunked verification (`RunMode::LimitCycles`).
3. The script runs until the chunk limit triggers `Pause` inside `iterate_inner`.
4. `iterate_outer` executes: commits VM cycles to `total_cycles`, resets `iteration_cycles = 0`, calls `process_io()` which charges N IO cycles into `iteration_cycles`, then `Pause` propagates.
5. `chunk_run` catches `Pause`, calls `scheduler.suspend()` — saves `total_cycles = T`, `iteration_cycles = N`.
6. Next chunk: `resume()` restores `total_cycles = T`, sets `iteration_cycles = 0` — N cycles are lost.
7. Scheduler believes only T cycles consumed; actual is T + N.
8. Script receives N extra cycles of budget per suspend/resume cycle.
9. Repeat across M chunks: M × N cycles of bypass accumulate, allowing the script to exceed the enforced limit. [9](#0-8) [10](#0-9)

### Citations

**File:** script/src/scheduler.rs (L156-158)
```rust
    pub fn consumed_cycles(&self) -> Cycle {
        self.total_cycles.load(Ordering::Acquire)
    }
```

**File:** script/src/scheduler.rs (L215-242)
```rust
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

**File:** script/src/scheduler.rs (L422-454)
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

**File:** script/src/verify.rs (L481-491)
```rust
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
```
