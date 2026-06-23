### Title
Incorrect Cycle Accounting in `resume_from_state` Inflates Reported Cycles, Causing Valid Transactions to Be Rejected — (File: `script/src/verify.rs`)

---

### Summary

In `TransactionScriptsVerifier::resume_from_state`, the loop over subsequent script groups (after the resumed group) uses `consumed_cycles` instead of `used_cycles` for the `cycles` accumulator. Because `consumed_cycles` includes VM suspension/resumption overhead from the Meepo hardfork, the returned cycle count is systematically inflated relative to actual execution cycles. This causes the tx-pool to over-report cycles for multi-group transactions verified via the chunked/resumable path, leading to valid transactions being incorrectly rejected as exceeding `max_tx_verify_cycles`.

---

### Finding Description

In `script/src/verify.rs`, the `resume_from_state` function handles resuming a suspended multi-group script verification. For the groups that follow the resumed group, the code reads:

```rust
// script/src/verify.rs lines 322–325
Ok(ChunkState::Completed(_, consumed_cycles)) => {
    current_used = wrapping_cycles_add(current_used, consumed_cycles, group)?;
    cycles = wrapping_cycles_add(cycles, consumed_cycles, group)?;
}
``` [1](#0-0) 

`used_cycles` is silently discarded (`_`) and `consumed_cycles` is used for both the limit-tracking variable (`current_used`) and the returned total (`cycles`).

Compare this with the analogous loop in `resumable_verify`, which correctly separates the two:

```rust
// script/src/verify.rs lines 242–245
Ok(ChunkState::Completed(used_cycles, consumed_cycles)) => {
    current_consumed_cycles =
        wrapping_cycles_add(current_consumed_cycles, consumed_cycles, group)?;
    cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
}
``` [2](#0-1) 

The distinction between `used_cycles` and `consumed_cycles` is intentional and documented in the scheduler. `consumed_cycles` includes overhead cycles charged for VM suspension and resumption during inter-VM IO processing (the Meepo hardfork behavior), while `used_cycles` reflects only actual execution cycles: [3](#0-2) 

Because `consumed_cycles >= used_cycles`, using `consumed_cycles` for `cycles` in `resume_from_state` inflates the reported total. The hardcoded cycle costs for spawn/exec/IO operations (`SPAWN_EXTRA_CYCLES_BASE = 100_000`, `EXEC_LOAD_ELF_V2_CYCLES_BASE = 75_000`, `SPAWN_YIELD_CYCLES_BASE = 800`) are fixed constants that contribute to this overhead gap: [4](#0-3) 

These fixed overhead costs are charged unconditionally on every spawn/exec/IO syscall, and the gap between `consumed_cycles` and `used_cycles` grows with the number of inter-VM operations in a transaction's scripts.

---

### Impact Explanation

A transaction sender submits a transaction whose lock or type scripts use the `spawn`/`exec`/pipe-IO syscalls (available since ScriptVersion V2 / Meepo hardfork). When the tx-pool verifies this transaction via the chunked resumable path (triggered when scripts exceed the per-iteration cycle budget), `resume_from_state` is called for subsequent script groups. The inflated `cycles` value returned by `resume_from_state` is compared against `max_tx_verify_cycles` (default 70,000,000): [5](#0-4) 

If the inflated count crosses this threshold, the tx-pool rejects the transaction with `ExceededMaximumCycles`, even though the actual execution cycles are within the limit. The transaction is permanently excluded from the pool and cannot be committed to the chain.

---

### Likelihood Explanation

Any unprivileged transaction sender can trigger this path by submitting a transaction whose scripts:
1. Use `spawn`/`exec`/pipe-IO syscalls (standard CKB-VM V2 features, no special privilege required).
2. Contain multiple script groups (e.g., multiple inputs with different lock scripts).
3. Have total execution cycles close to `max_tx_verify_cycles`.

The chunked verification path is activated automatically by the tx-pool when a script group's cycles exceed the per-step budget. The attacker does not need to know internal node state; they only need to craft a transaction whose scripts legitimately consume cycles near the limit. The overhead gap from `SPAWN_EXTRA_CYCLES_BASE` (100,000 cycles per spawn call) means even a modest number of spawn calls can push the inflated count over the threshold.

---

### Recommendation

In `resume_from_state`, restore the correct separation between `used_cycles` (for the returned total) and `consumed_cycles` (for limit tracking), matching the pattern used in `resumable_verify`:

```rust
// Fix for script/src/verify.rs lines 322–325
Ok(ChunkState::Completed(used_cycles, consumed_cycles)) => {
    current_used = wrapping_cycles_add(current_used, consumed_cycles, group)?;
    cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
}
```

Additionally, audit `complete()` at line 395 where `current_group` is used instead of `group` in the loop body, causing incorrect error-source attribution on `CyclesOverflow`: [6](#0-5) 

---

### Proof of Concept

1. Construct a transaction with two lock-script groups, each using `ckb_spawn` to launch a child VM that performs pipe IO (charging `SPAWN_EXTRA_CYCLES_BASE + SPAWN_YIELD_CYCLES_BASE` overhead per call).
2. Tune the scripts so that actual `used_cycles` ≈ `max_tx_verify_cycles - 1` but `consumed_cycles` (with overhead) > `max_tx_verify_cycles`.
3. Submit the transaction via `send_transaction` RPC.
4. The tx-pool invokes chunked verification; `resume_from_state` is called for the second script group.
5. The inflated `cycles` value causes `Reject::DeclaredWrongCycles` or `ExceededMaximumCycles`, and the transaction is rejected despite being valid.
6. Confirm by running the same transaction through `verify()` (non-chunked path), which accepts it successfully.

### Citations

**File:** script/src/verify.rs (L242-245)
```rust
                Ok(ChunkState::Completed(used_cycles, consumed_cycles)) => {
                    current_consumed_cycles =
                        wrapping_cycles_add(current_consumed_cycles, consumed_cycles, group)?;
                    cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
```

**File:** script/src/verify.rs (L322-325)
```rust
            match self.verify_group_with_chunk(group, remain_cycles, &None) {
                Ok(ChunkState::Completed(_, consumed_cycles)) => {
                    current_used = wrapping_cycles_add(current_used, consumed_cycles, group)?;
                    cycles = wrapping_cycles_add(cycles, consumed_cycles, group)?;
```

**File:** script/src/verify.rs (L393-396)
```rust
            match self.verify_group_with_chunk(group, remain_cycles, &None) {
                Ok(ChunkState::Completed(used_cycles, _consumed_cycles)) => {
                    cycles = wrapping_cycles_add(cycles, used_cycles, current_group)?;
                }
```

**File:** script/src/scheduler.rs (L86-95)
```rust
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
