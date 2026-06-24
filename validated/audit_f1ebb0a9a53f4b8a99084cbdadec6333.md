Audit Report

## Title
`Scheduler::resume()` Unconditionally Zeroes `iteration_cycles` Saved by `suspend()`, Causing Permanent Cycle-Accounting Undercount — (File: `script/src/scheduler.rs`)

## Summary

`Scheduler::resume()` restores `iteration_cycles` from `FullSuspendedState` at line 216, but then unconditionally overwrites it with `0` at line 242. The codebase explicitly acknowledges that `iteration_cycles` is non-zero at suspension boundaries due to `process_io()` charging cycles after the per-iteration reset in `iterate_outer`. Those cycles are faithfully saved by `suspend()` but silently discarded by `resume()`, permanently understating consumed cycles across every suspend/resume boundary.

## Finding Description

**Cycle accumulator invariant.** At any instant, total consumed cycles = `total_cycles` + `iteration_cycles` + `machine.cycles()`. The field documentation at lines 59–97 and the `FullSuspendedState` documentation at lines 498–501 both explicitly state that `iteration_cycles` will not always be zero at visible execution boundaries due to a known Meepo hardfork behavior.

**How non-zero `iteration_cycles` arises at suspension.** In `iterate_outer` (lines 417–456):

1. `iterate_inner` runs; its result is stored in `iterate_return` (not yet propagated).
2. `consume_cycles(self.iteration_cycles)` commits current `iteration_cycles` into `total_cycles`.
3. `self.iteration_cycles = 0` — reset.
4. `self.process_io()?` — runs **after** the reset; charges IO-operation cycles directly into `iteration_cycles`.
5. `let id = iterate_return?` — if `iterate_return` is `Err(Pause)`, the error propagates here, leaving `iteration_cycles` non-zero from step 4.

**`suspend()` faithfully saves the non-zero value** (line 267: `iteration_cycles: self.iteration_cycles`).

**`resume()` discards it** (lines 216 and 242):
```rust
iteration_cycles: full.iteration_cycles,   // line 216: restored
// ...
scheduler.iteration_cycles = 0;            // line 242: unconditionally zeroed
```
The comment at lines 239–241 justifies zeroing to avoid charging cycles for `ensure_vms_instantiated` overhead, but the reset also silently discards the pre-suspension `iteration_cycles` that represent real script execution work not yet reflected in `total_cycles`.

**Exploit path in `chunk_run` / `resume_from_state`:**
- `chunk_run` (lines 470–510) catches `Pause`, calls `scheduler.suspend()`, returns `ChunkState::Suspended`.
- `resume_from_state` (lines 276–341) loops, calling `verify_group_with_chunk` → `chunk_run` → `resume_scheduler` → `Scheduler::resume()` on each chunk.
- Each resume discards the IO cycles accumulated in the final `process_io()` call of the previous chunk.

## Impact Explanation

After each resume, `total_cycles` is understated by the IO cycles discarded. The tx-pool's chunked cycle-limit enforcement computes `remaining_cycles = limit_cycles - iteration_cycles` (line 424–426); because the lost cycles appear in neither `total_cycles` nor `iteration_cycles`, the scheduler believes it has more budget than it actually does. Across N suspend/resume cycles, the bypass accumulates as `N × (IO cycles per process_io call)`. Transactions that should be rejected for exceeding the cycle limit are accepted into the tx-pool. If a miner includes such a transaction, consensus-layer non-resumable verification rejects the block, wasting mining effort and causing chain-tip instability.

This matches the allowed impact: **High — Incorrect implementation or behavior of CKB-VM or system scripts.**

## Likelihood Explanation

Any unprivileged transaction submitter can trigger this. The attacker submits a transaction that:
1. Uses `spawn`/`pipe` syscalls so that pending IO read/write pairs exist.
2. Runs long enough to trigger the tx-pool's chunked verification (`ChunkCommand::Suspend` → `Pause`).
3. At the `Pause` point, `process_io()` has already charged IO cycles into `iteration_cycles`.
4. On the next chunk, `resume()` discards those cycles.

No key material, privileged access, or majority hashpower is required. The bypass is repeatable across every suspend/resume boundary and compounds linearly with the number of chunks.

## Recommendation

In `resume()`, preserve the pre-suspension `iteration_cycles` after `ensure_vms_instantiated` completes. Only the cycles added by `ensure_vms_instantiated` itself should be discarded:

```rust
let saved_iteration_cycles = full.iteration_cycles;
// ... build scheduler with iteration_cycles: full.iteration_cycles ...
scheduler.ensure_vms_instantiated(&full.instantiated_ids).unwrap();
// Discard only cycles from ensure_vms_instantiated (resume overhead),
// restore pre-suspension iteration_cycles not yet in total_cycles.
scheduler.iteration_cycles = saved_iteration_cycles;
```

Alternatively, `suspend()` should flush `iteration_cycles` into `total_cycles` before saving state, so `iteration_cycles` is always zero at suspension boundaries and the reset in `resume()` is safe. This would require adjusting the Meepo hardfork behavior preservation logic.

## Proof of Concept

**Minimal trace:**

1. `iterate_inner` runs: VM executes 800 cycles → `iteration_cycles = 800`.
2. `consume_cycles(800)` → `total_cycles = 800`; `iteration_cycles = 0`.
3. `process_io()` charges 50 IO cycles → `iteration_cycles = 50`.
4. `iterate_return?` propagates `Err(Pause)`.
5. `chunk_run` catches `Pause`, calls `scheduler.suspend()`.
6. `suspend()` saves: `total_cycles = 800`, `iteration_cycles = 50`.
7. `resume()` restores `total_cycles = 800`, sets `iteration_cycles = 0`. **50 cycles permanently lost.**
8. Next chunk runs with `limit_cycles = 1000`. Scheduler believes 800 cycles consumed; actual is 850. Script gets 50 extra cycles per boundary.

**Test plan:** Write a unit test that creates a scheduler with spawn/pipe VMs, runs it until `Pause`, calls `suspend()`, then `resume()`, and asserts that `scheduler.consumed_cycles()` after resume equals the value before suspend. The assertion will fail, confirming the undercount. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** script/src/scheduler.rs (L215-216)
```rust
            total_cycles: Arc::new(AtomicU64::new(full.total_cycles)),
            iteration_cycles: full.iteration_cycles,
```

**File:** script/src/scheduler.rs (L239-242)
```rust
        // NOTE: suspending/resuming a scheduler is part of CKB's implementation
        // details. It is not part of execution consensue. We should not charge
        // cycles for them.
        scheduler.iteration_cycles = 0;
```

**File:** script/src/scheduler.rs (L265-267)
```rust
            // a VM in the process of suspending the whole scheduler.
            total_cycles: self.total_cycles.load(Ordering::Acquire),
            iteration_cycles: self.iteration_cycles,
```

**File:** script/src/scheduler.rs (L422-428)
```rust
        let iterate_return = self.iterate_inner(pause.clone(), limit_cycles);
        self.consume_cycles(self.iteration_cycles)?;
        let remaining_cycles = limit_cycles
            .checked_sub(self.iteration_cycles)
            .ok_or(Error::CyclesExceeded)?;
        // Clear iteration cycles intentionally after each run
        self.iteration_cycles = 0;
```

**File:** script/src/scheduler.rs (L453-455)
```rust
        self.process_io()?;
        let id = iterate_return?;
        Ok((id, remaining_cycles))
```

**File:** script/src/types.rs (L498-501)
```rust
    /// Iteration cycles. Due to an implementation bug in Meepo hardfork,
    /// this value will not always be zero at visible execution boundaries.
    /// We will have to preserve this value.
    pub iteration_cycles: Cycle,
```

**File:** script/src/verify.rs (L500-505)
```rust
            Err(error) => match error {
                VMInternalError::CyclesExceeded | VMInternalError::Pause => {
                    let snapshot = scheduler
                        .suspend()
                        .map_err(|err| self.map_vm_internal_error(err, max_cycles))?;
                    Ok(ChunkState::suspended(snapshot))
```
