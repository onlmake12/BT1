### Title
`current_cycles` Syscall Returns Stale/Incorrect Cycle Count Due to Unaccounted `iteration_cycles` After IO Processing — (`File: script/src/scheduler.rs`)

### Summary

In the CKB-VM `Scheduler` (Meepo hardfork / ScriptVersion::V2), cycles consumed by VM suspend/resume operations during `process_io()` are accumulated into `iteration_cycles` **after** that field has already been zeroed and flushed to `total_cycles`. Because the `current_cycles` syscall reads `total_cycles` directly, any spawned child VM that calls `current_cycles` in the immediately following iteration receives a cycle count that is lower than the true consumed amount by exactly the IO-processing overhead cycles (`SPAWN_EXTRA_CYCLES_BASE` per resume). This is the direct CKB analog of the reported "initial value of 0 causes accumulator to always return 0" pattern: an intermediate accumulator (`iteration_cycles`) is reset before a secondary charging path writes into it, so the secondary charges are invisible to the next reader of the shared counter.

### Finding Description

In `script/src/scheduler.rs`, `iterate_outer` follows this sequence:

```
1. iterate_inner(...)          // VM runs; charges go into iteration_cycles
2. consume_cycles(iteration_cycles)  // flush iteration_cycles → total_cycles
3. iteration_cycles = 0        // ← reset
4. process_io()                // ← resume_vm() adds SPAWN_EXTRA_CYCLES_BASE
                               //   to iteration_cycles AFTER the reset
``` [1](#0-0) 

Inside `resume_vm()`, called from `process_io()`:

```rust
self.iteration_cycles = self
    .iteration_cycles
    .checked_add(SPAWN_EXTRA_CYCLES_BASE)
    .ok_or(Error::CyclesExceeded)?;
``` [2](#0-1) 

These newly accumulated `iteration_cycles` are **not** flushed to `total_cycles` until the *next* call to `consume_cycles` in the next `iterate_outer` invocation.

The `current_cycles` syscall reads `base_cycles`, which is `Arc::clone(&self.total_cycles)`:

```rust
let cycles = self.base.load(Ordering::Acquire)
    .checked_add(machine.cycles())
    .ok_or(VMError::CyclesOverflow)?;
``` [3](#0-2) 

`base_cycles` is wired to `total_cycles` at VM creation time:

```rust
let vm_context = VmContext {
    base_cycles: Arc::clone(&self.total_cycles),
    ...
};
``` [4](#0-3) 

So when the next VM (e.g., a freshly spawned child) calls `current_cycles`, it reads `total_cycles` which does **not yet** include the `SPAWN_EXTRA_CYCLES_BASE` charges from the preceding `process_io()`. The returned value is lower than the true consumed cycle count by that amount.

The codebase explicitly acknowledges this as a bug preserved for consensus compatibility:

> "our initial implementation for Meepo hardfork contains a bug: cycles charged by suspending / resuming VMs when processing IOs, will not be reflected in `current cycles` syscalls of the subsequent running VMs." [5](#0-4) 

The `FullSuspendedState` even preserves the non-zero `iteration_cycles` across suspend/resume cycles specifically because of this behavior: [6](#0-5) 

### Impact Explanation

Any script running under ScriptVersion::V2 (Meepo hardfork) that:
1. Uses `spawn` to create child VMs, and
2. Relies on the `current_cycles` syscall (syscall number `2042`) to make decisions about remaining cycle budget or to pass cycle-count information to child processes

will receive an incorrect (understated) cycle count. A child VM that checks its own cycle budget via `current_cycles` and makes branching decisions based on it (e.g., "do I have enough cycles to complete this critical path?") will operate on stale data. The test `spawn_create_17_spawn` documents and pins the exact incorrect cycle value (`36445673`) to prevent regressions, confirming the behavior is observable and reproducible. [7](#0-6) 

### Likelihood Explanation

Any unprivileged script author submitting a transaction with a lock/type script that uses `spawn` (available since ScriptVersion::V2) and calls `current_cycles` in a spawned child process will trigger this path. No special privileges, keys, or network position are required. The entry path is: **script author → transaction submission → CKB-VM script execution → spawn syscall → current_cycles syscall in child VM**.

### Recommendation

After `process_io()` completes in `iterate_outer`, flush the newly accumulated `iteration_cycles` to `total_cycles` before the next VM iteration begins, so that `current_cycles` reads a value that includes all IO-processing overhead. This was intentionally deferred for Meepo hardfork consensus compatibility, but should be corrected in a subsequent hardfork version as the code itself notes.

### Proof of Concept

The existing regression test documents the exact manifestation:

```rust
// This test documents a bug in Meepo hardfork version: when IO processing
// code suspends or resumes any VMs, the cycles consumed by suspending / resuming
// VMs will not be reflected by `current cycles` syscall in the immediate
// subsequent VM execution.
fn spawn_create_17_spawn() { ... assert_eq!(cycles, 36445673); }
``` [7](#0-6) 

A script author can craft a child script that calls `current_cycles` immediately after being spawned and asserts the returned value is greater than a known threshold. Due to the missing `SPAWN_EXTRA_CYCLES_BASE` charges in `total_cycles`, the assertion will fail or the child will make incorrect cycle-budget decisions, demonstrating the stale accumulator state — the direct analog of the reported `calculateAverage()` always returning 0 due to an uninitialized/reset accumulator.

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

**File:** script/src/scheduler.rs (L954-957)
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

**File:** script/src/syscalls/current_cycles.rs (L37-41)
```rust
        let cycles = self
            .base
            .load(Ordering::Acquire)
            .checked_add(machine.cycles())
            .ok_or(VMError::CyclesOverflow)?;
```

**File:** script/src/types.rs (L498-501)
```rust
    /// Iteration cycles. Due to an implementation bug in Meepo hardfork,
    /// this value will not always be zero at visible execution boundaries.
    /// We will have to preserve this value.
    pub iteration_cycles: Cycle,
```

**File:** script/src/verify/tests/ckb_latest/features_since_v2023.rs (L1544-1581)
```rust
// This test documents a bug in Meepo hardfork version: when IO processing
// code suspends or resumes any VMs, the cycles consumed by suspending / resuming
// VMs will not be reflected by `current cycles` syscall in the immediate
// subsequent VM execution. Here we are asserting the exact cycles consumed
// by a program touching this behavior, so as to prevent any future regressions.
#[test]
fn spawn_create_17_spawn() {
    if SCRIPT_VERSION < ScriptVersion::V2 {
        return;
    }
    let script_version = ScriptVersion::V2;

    let (cell, data_hash) = load_cell_from_path("testdata/spawn_create_17_spawn");
    let script = Script::new_builder()
        .hash_type(script_version.data_hash_type())
        .code_hash(data_hash)
        .build();
    let output = CellOutputBuilder::default()
        .capacity(capacity_bytes!(100))
        .lock(script)
        .build();
    let input = CellInput::new(OutPoint::null(), 0);

    let transaction = TransactionBuilder::default().input(input).build();
    let dummy_cell = create_dummy_cell(output);

    let rtx = ResolvedTransaction {
        transaction,
        resolved_cell_deps: vec![cell],
        resolved_inputs: vec![dummy_cell],
        resolved_dep_groups: vec![],
    };
    let verifier = TransactionScriptsVerifierWithEnv::new();
    let cycles = verifier
        .verify_without_limit(script_version, &rtx)
        .expect("verify");

    assert_eq!(cycles, 36445673);
```
