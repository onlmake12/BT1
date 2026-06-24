Audit Report

## Title
`resume_from_state` Uses `consumed_cycles` Instead of `used_cycles` for Total Cycle Accounting, Inflating Reported Cycles for Multi-Group Spawn Transactions — (File: `script/src/verify.rs`)

## Summary
In `TransactionScriptsVerifier::resume_from_state`, the loop over script groups following the resumed group discards `used_cycles` and uses `consumed_cycles` for both the limit-tracking variable and the returned total. Because `consumed_cycles` includes spawn/exec/pipe-IO overhead not present in `used_cycles`, the returned cycle count is inflated for any transaction whose subsequent script groups use ScriptVersion V2 syscalls. This causes the tx-pool to reject valid transactions via the chunked verification path when actual execution cycles are within `max_tx_verify_cycles` but the inflated count is not.

## Finding Description
In `script/src/verify.rs`, `ChunkState::Completed` carries two distinct cycle values:

```rust
// (total_cycles, consumed_cycles in last chunk)
Completed(Cycle, Cycle),
``` [1](#0-0) 

`chunk_run` populates these as: first field = `TerminatedResult.consumed_cycles` (execution cycles as seen by the VM), second field = `scheduler.consumed_cycles() - previous_cycles` (execution + spawn/exec/IO overhead). [2](#0-1) 

`resumable_verify` correctly separates the two: `used_cycles` (first field) accumulates into the returned total `cycles`, while `consumed_cycles` (second field) tracks the limit budget `current_consumed_cycles`: [3](#0-2) 

`resume_from_state` handles the resumed group correctly at lines 299–301 (uses `used_cycles` for `cycles`, `consumed_cycles` for `current_used`). However, the subsequent-groups loop at lines 322–325 discards `used_cycles` entirely and uses `consumed_cycles` for **both** accumulators: [4](#0-3) 

The scheduler documentation confirms that `consumed_cycles` includes overhead cycles charged during VM suspension/resumption for inter-VM IO (Meepo hardfork behavior), which are intentionally not reflected in the VM's own `current_cycles` view: [5](#0-4) 

The fixed overhead constants (`SPAWN_EXTRA_CYCLES_BASE = 100_000`, `SPAWN_YIELD_CYCLES_BASE = 800`, `EXEC_LOAD_ELF_V2_CYCLES_BASE = 75_000`) mean the gap between `consumed_cycles` and `used_cycles` grows with each spawn/exec/IO call: [6](#0-5) 

The inflated `cycles` value is returned as `VerifyResult::Completed(cycles)` and propagated to the tx-pool, which compares it against `max_tx_verify_cycles`: [7](#0-6) [8](#0-7) 

Existing tests (`_check_typical_secp256k1_blake160_2_in_2_out_resume_load_cycles`, `check_spawn_state`) do not cover the combination of multiple script groups where subsequent groups use spawn/exec/IO, so the bug is not caught by the test suite. The secp256k1 tests are unaffected because `consumed_cycles == used_cycles` for scripts without spawn/exec/IO.

## Impact Explanation
This is an incorrect implementation of CKB-VM script cycle accounting (High, 10001–15000 points). A valid transaction whose scripts legitimately use spawn/exec/pipe-IO syscalls across multiple script groups will be rejected by the tx-pool via the chunked verification path with `ExceededMaximumCycles`, even though the same transaction passes the non-chunked `verify()` path. Since the tx-pool is the standard propagation mechanism, the transaction cannot reach miners through normal network operation.

## Likelihood Explanation
Any unprivileged user can trigger this by submitting a transaction that: (1) has two or more script groups (e.g., multiple inputs with different lock scripts), (2) uses `ckb_spawn`/`ckb_exec`/pipe-IO syscalls in the script groups following the first, and (3) has total actual execution cycles close to `max_tx_verify_cycles`. The chunked path is activated automatically when a script group's cycles exceed the per-step budget. No special privileges, leaked keys, or victim mistakes are required. The overhead gap from even a modest number of spawn calls (each adding 100,000 overhead cycles) is sufficient to push the inflated count over the threshold.

## Recommendation
In `resume_from_state`, restore the correct separation between `used_cycles` (for the returned total) and `consumed_cycles` (for limit tracking), matching the pattern in `resumable_verify`:

```rust
// script/src/verify.rs lines 322–325 — fix
Ok(ChunkState::Completed(used_cycles, consumed_cycles)) => {
    current_used = wrapping_cycles_add(current_used, consumed_cycles, group)?;
    cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
}
```

Additionally, fix the unrelated error-attribution bug in `complete()` at line 395, where `current_group` is used instead of `group` inside the subsequent-groups loop, causing incorrect `CyclesOverflow` error source attribution: [9](#0-8) 

## Proof of Concept
1. Construct a transaction with two lock-script groups. The second group's script calls `ckb_spawn` to launch a child VM that performs pipe IO, charging `SPAWN_EXTRA_CYCLES_BASE + SPAWN_YIELD_CYCLES_BASE` overhead per call.
2. Tune the scripts so that actual `used_cycles` ≈ `max_tx_verify_cycles - 1` but `consumed_cycles` (with overhead) > `max_tx_verify_cycles`.
3. Submit via `send_transaction` RPC. The tx-pool invokes chunked verification; `resumable_verify` suspends on the first group, then `resume_from_state` is called. After the first group completes, the subsequent-groups loop runs with the buggy accounting.
4. Observe `ExceededMaximumCycles` rejection.
5. Confirm by running the same transaction through `verifier.verify()` (non-chunked path), which accepts it successfully — demonstrating the discrepancy between the two paths.
6. A unit test mirroring `_check_typical_secp256k1_blake160_2_in_2_out_resume_load_cycles` but using spawn scripts and asserting `cycles == cycles_once` will fail on the current code and pass after the fix.

### Citations

**File:** script/src/verify.rs (L38-42)
```rust
pub enum ChunkState {
    Suspended(Option<FullSuspendedState>),
    // (total_cycles, consumed_cycles in last chunk)
    Completed(Cycle, Cycle),
}
```

**File:** script/src/verify.rs (L241-246)
```rust
            match self.verify_group_with_chunk(group, remain_cycles, &None) {
                Ok(ChunkState::Completed(used_cycles, consumed_cycles)) => {
                    current_consumed_cycles =
                        wrapping_cycles_add(current_consumed_cycles, consumed_cycles, group)?;
                    cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
                }
```

**File:** script/src/verify.rs (L322-326)
```rust
            match self.verify_group_with_chunk(group, remain_cycles, &None) {
                Ok(ChunkState::Completed(_, consumed_cycles)) => {
                    current_used = wrapping_cycles_add(current_used, consumed_cycles, group)?;
                    cycles = wrapping_cycles_add(cycles, consumed_cycles, group)?;
                }
```

**File:** script/src/verify.rs (L338-341)
```rust
        }

        Ok(VerifyResult::Completed(cycles))
    }
```

**File:** script/src/verify.rs (L393-396)
```rust
            match self.verify_group_with_chunk(group, remain_cycles, &None) {
                Ok(ChunkState::Completed(used_cycles, _consumed_cycles)) => {
                    cycles = wrapping_cycles_add(cycles, used_cycles, current_group)?;
                }
```

**File:** script/src/verify.rs (L481-492)
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
                    ))
```

**File:** script/src/scheduler.rs (L88-95)
```rust
    /// NOTE: the above workflow describes the optimal case: `iteration_cycles`
    /// will always be zero after each iteration. However, our initial implementation
    /// for Meepo hardfork contains a bug: cycles charged by suspending / resuming
    /// VMs when processing IOs, will not be reflected in `current cycles` syscalls
    /// of the subsequent running VMs. To preserve this behavior, consumed cycles in
    /// iteration_cycles cannot be moved at iterate boundaries. Later hardfork versions
    /// might fix this, but for the Meepo hardfork, we will have to preserve this behavior.
    total_cycles: Arc<AtomicU64>,
```

**File:** script/src/syscalls/mod.rs (L105-107)
```rust
pub const EXEC_LOAD_ELF_V2_CYCLES_BASE: u64 = 75_000;
pub const SPAWN_EXTRA_CYCLES_BASE: u64 = 100_000;
pub const SPAWN_YIELD_CYCLES_BASE: u64 = 800;
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```
