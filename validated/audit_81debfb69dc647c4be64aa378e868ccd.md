### Title
`ChunkState::Suspended(None)` Conflates Resumable Suspension with TypeID Cycle-Exceeded State — (File: `script/src/verify.rs`)

---

### Summary

`ChunkState::Suspended(Option<FullSuspendedState>)` uses `None` as a silent sentinel for a TypeID script that exceeded its cycle limit, making it indistinguishable from a genuine mid-execution suspension. Callers of `resumable_verify` and `resume_from_state` receive `VerifyResult::Suspended` in both cases and have no way to differentiate "suspended with recoverable state" from "TypeID exceeded cycles and cannot be resumed at this limit." This is the direct CKB analog of the `canExecute` boolean ambiguity: a two-value representation is used where at least three distinct states exist.

---

### Finding Description

In `script/src/verify.rs`, the `ChunkState` enum is defined as:

```rust
pub enum ChunkState {
    Suspended(Option<FullSuspendedState>),
    Completed(Cycle, Cycle),
}
```

Two constructors exist:
- `ChunkState::suspended(state)` → `Suspended(Some(state))` — genuine mid-execution pause with a serializable VM snapshot.
- `ChunkState::suspended_type_id()` → `Suspended(None)` — used exclusively when a TypeID system script exceeds its cycle limit. [1](#0-0) 

The root cause is in `verify_group_with_chunk`. When a TypeID script exceeds cycles, instead of propagating an error, the function converts the failure into `Ok(ChunkState::suspended_type_id())`:

```rust
Err(ScriptError::ExceededMaximumCycles(_)) => Ok(ChunkState::suspended_type_id()),
``` [2](#0-1) 

In `resumable_verify`, both `Suspended(Some(...))` and `Suspended(None)` are handled by the same arm — both produce a `TransactionState` and return `VerifyResult::Suspended`:

```rust
Ok(ChunkState::Suspended(state)) => {
    let current = idx;
    let state = TransactionState::new(state, current, cycles, remain_cycles);
    return Ok(VerifyResult::Suspended(state));
}
``` [3](#0-2) 

This creates a `TransactionState { state: None, current: idx, ... }`. When `resume_from_state` is subsequently called with this state, it passes `state = &None` to `verify_group_with_chunk`. For TypeID, the `state` parameter is entirely ignored — the script always re-runs from scratch: [4](#0-3) 

If the TypeID script still exceeds the cycle limit, `verify_group_with_chunk` returns `Suspended(None)` again. The caller receives `VerifyResult::Suspended` again, with no indication that the state is non-resumable. The `TransactionState::next_limit_cycles` helper will eventually converge `limit_cycles` to `max_cycles`, but because TypeID always restarts from scratch (ignoring the `state` field), `current_cycles` in the `TransactionState` is never advanced, so `remain = max_cycles - 0 = max_cycles` on every iteration — producing an infinite loop of `VerifyResult::Suspended` returns. [5](#0-4) 

The `complete()` method does handle this correctly — it treats any `Suspended` as `ExceededMaximumCycles` — but `resumable_verify` and `resume_from_state` do not. [6](#0-5) 

---

### Impact Explanation

Any component that drives the `resumable_verify` → `resume_from_state` loop against a transaction containing a TypeID script whose cycle requirement exceeds the step limit will spin indefinitely, consuming CPU without making progress or returning an error. The caller cannot distinguish the non-resumable `Suspended(None)` from a legitimate `Suspended(Some(...))` without inspecting the inner `Option`, which the public `VerifyResult::Suspended(TransactionState)` API does not expose directly. This is a denial-of-service against the verification worker that processes such a transaction.

---

### Likelihood Explanation

TypeID scripts (`TYPE_ID_CODE_HASH` with `ScriptHashType::Type`) are a standard CKB feature available to any transaction author. A script author can craft a transaction that uses a TypeID cell and submit it to a node running the `resumable_verify` path. The `resumable_verify` function is defined in `script/src/verify.rs` and has a call site in `verification/src/transaction_verifier.rs`, placing it within the verification pipeline reachable from block or transaction processing. The cycle cost of TypeID is fixed and known, so an attacker can reliably target step-cycle limits smaller than `TYPE_ID_CYCLES` to trigger the condition.

---

### Recommendation

Replace the overloaded `Option<FullSuspendedState>` with an explicit three-variant enum to eliminate the ambiguity:

```rust
pub enum ChunkState {
    Completed(Cycle, Cycle),
    Suspended(FullSuspendedState),
    TypeIdExceededCycles,
}
```

Update `resumable_verify` and `resume_from_state` to treat `TypeIdExceededCycles` as an error (`ScriptError::ExceededMaximumCycles`) rather than a resumable suspension. Alternatively, `verify_group_with_chunk` should propagate `ExceededMaximumCycles` as an `Err` for TypeID scripts rather than converting it to `Ok(Suspended(None))`, consistent with how all other script errors are handled.

---

### Proof of Concept

1. Craft a transaction with a TypeID cell (lock or type script using `TYPE_ID_CODE_HASH` + `ScriptHashType::Type`).
2. Call `resumable_verify(step_cycles)` where `step_cycles < TYPE_ID_CYCLES`.
3. Receive `Ok(VerifyResult::Suspended(state))` where `state.state == None`.
4. Call `resume_from_state(&state, step_cycles * 2)` — TypeID ignores `state`, re-runs from scratch, still exceeds cycles.
5. Receive `Ok(VerifyResult::Suspended(state))` again with `state.state == None` and `state.current_cycles` unchanged.
6. `next_limit_cycles` computes `remain = max_cycles - 0 = max_cycles` on every iteration.
7. Once `limit_cycles` reaches `max_cycles`, TypeID still returns `Suspended(None)` (cycle limit is `max_cycles`, TypeID needs `> max_cycles`).
8. The loop never terminates and never returns an error. [7](#0-6) [1](#0-0)

### Citations

**File:** script/src/verify.rs (L38-51)
```rust
pub enum ChunkState {
    Suspended(Option<FullSuspendedState>),
    // (total_cycles, consumed_cycles in last chunk)
    Completed(Cycle, Cycle),
}

impl ChunkState {
    pub fn suspended(state: FullSuspendedState) -> Self {
        ChunkState::Suspended(Some(state))
    }

    pub fn suspended_type_id() -> Self {
        ChunkState::Suspended(None)
    }
```

**File:** script/src/verify.rs (L247-251)
```rust
                Ok(ChunkState::Suspended(state)) => {
                    let current = idx;
                    let state = TransactionState::new(state, current, cycles, remain_cycles);
                    return Ok(VerifyResult::Suspended(state));
                }
```

**File:** script/src/verify.rs (L295-306)
```rust
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
```

**File:** script/src/verify.rs (L375-379)
```rust
            Ok(ChunkState::Suspended(_)) => {
                return Err(ScriptError::ExceededMaximumCycles(max_cycles)
                    .source(current_group)
                    .into());
            }
```

**File:** script/src/verify.rs (L446-468)
```rust
    fn verify_group_with_chunk(
        &self,
        group: &ScriptGroup,
        max_cycles: Cycle,
        state: &Option<FullSuspendedState>,
    ) -> Result<ChunkState, ScriptError> {
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
        {
            let verifier = TypeIdSystemScript {
                rtx: &self.tx_data.rtx,
                script_group: group,
                max_cycles,
            };
            match verifier.verify() {
                Ok(cycles) => Ok(ChunkState::Completed(cycles, cycles)),
                Err(ScriptError::ExceededMaximumCycles(_)) => Ok(ChunkState::suspended_type_id()),
                Err(e) => Err(e),
            }
        } else {
            self.chunk_run(group, max_cycles, state)
        }
    }
```

**File:** script/src/types.rs (L230-239)
```rust
    pub fn next_limit_cycles(&self, step_cycles: Cycle, max_cycles: Cycle) -> (Cycle, bool) {
        let remain = max_cycles - self.current_cycles;
        let next_limit = self.limit_cycles + step_cycles;

        if next_limit < remain {
            (next_limit, false)
        } else {
            (remain, true)
        }
    }
```
