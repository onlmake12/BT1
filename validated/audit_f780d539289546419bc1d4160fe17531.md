### Title
CKB-VM Scheduler Write-Write Deadlock Permanently Locks Cell Funds — (File: `script/src/scheduler.rs`)

---

### Summary

The CKB-VM `Scheduler` introduced in the Meepo hardfork manages multiple spawned VMs communicating via pipes. When all active VMs simultaneously enter `VmState::WaitForWrite` (a write-write deadlock), the `process_io()` function cannot unblock any of them, and `iterate_prepare_machine()` returns `Error::Unexpected("A deadlock situation has been reached!")`. This causes script verification to fail permanently. A script author can craft a lock script that deterministically triggers this deadlock, permanently locking any CKB tokens in cells protected by that script with no protocol-level escape hatch.

---

### Finding Description

The `Scheduler` in `script/src/scheduler.rs` runs a cooperative multi-VM loop. Each VM can be in one of the states defined in `VmState`: [1](#0-0) 

The main execution loop in `run()` calls `iterate_outer()` until the root VM terminates: [2](#0-1) 

At the start of each iteration, `iterate_prepare_machine()` searches for a runnable VM. If none exists, it returns a hard error: [3](#0-2) 

After each VM runs, `process_io()` is called to resolve pending reads and writes. A VM in `WaitForWrite` is only unblocked in two cases:

1. The other end of its fd is **closed** → added to `closed_fds`, unblocked with partial write count.
2. The other end is in `WaitForRead` → data is transferred, both sides potentially unblocked. [4](#0-3) 

**The gap**: if all VMs are in `WaitForWrite` simultaneously (write-write deadlock), neither condition is met. The other end of each fd is still open (owned by another VM also in `WaitForWrite`), so `closed_fds` is not populated. And since no VM is in `WaitForRead`, `pairs` is also empty. `process_io()` makes no progress, and the next call to `iterate_prepare_machine()` returns the deadlock error.

This exact scenario is demonstrated by the test data: [5](#0-4) 

Both parent and child write to their stdout fds without reading from stdin — a classic write-write deadlock.

The deadlock error propagates through `run()` → `detailed_run()` → `verify_script_group()` and is mapped to `ScriptError::VMInternalError`: [6](#0-5) 

This causes the transaction to be rejected with `TransactionFailedToVerify`.

---

### Impact Explanation

In CKB's cell model, a cell's lock script is immutable after creation. If a lock script deterministically deadlocks, **every spend attempt on that cell will fail** with `ScriptError::VMInternalError`. There is no protocol-level escape hatch — no alternative code path, no admin override, no timeout-based fallback — that allows the cell to be spent. CKB tokens stored in such cells are permanently inaccessible. This is the direct analog to the PeriodicPrizeStrategy locked state: the state machine (the VM scheduler) enters a waiting state from which it cannot exit, and user funds are permanently locked.

---

### Likelihood Explanation

The spawn/pipe syscall API is available to any script author targeting the Meepo hardfork VM version. A script author can intentionally craft a lock script that creates a write-write deadlock. Users who send CKB to an address derived from such a script will have their funds permanently locked. The deadlock is deterministic and consensus-critical — every full node reaches the same state and rejects the spend transaction. There is no on-chain mechanism to detect or warn about a deadlocking lock script before funds are committed to it.

---

### Recommendation

1. **Deadlock detection in `process_io()`**: Before returning from `process_io()`, check whether all remaining VMs are in non-`Runnable` states with no IO progress made in the current round. If so, return a dedicated `Error::Deadlock` variant rather than deferring detection to `iterate_prepare_machine()`. This makes the failure mode explicit and distinguishable.
2. **Static analysis tooling**: Provide or document tooling that can detect write-write and wait-cycle deadlocks in spawn/pipe scripts before deployment.
3. **Distinct error code**: The current `Error::Unexpected("A deadlock situation has been reached!")` string is indistinguishable from other unexpected errors at the `ScriptError::VMInternalError` level. A dedicated error variant would allow wallets and explorers to surface a clearer diagnostic.

---

### Proof of Concept

**Deadlock construction** (mirrors `parent_write_dead_lock` / `child_write_dead_lock` in `script/testdata/spawn_cases.c`):

1. Root VM (parent) calls `ckb_pipe` to create `(fd_write, fd_read)`, then `ckb_spawn` passing `fd_write` to the child.
2. Parent calls `ckb_write(fd_write, data, &len)` → scheduler sets parent state to `VmState::WaitForWrite`.
3. Child calls `ckb_write(inherited_fd_write, data, &len)` → scheduler sets child state to `VmState::WaitForWrite`.
4. `process_io()` is called:
   - Parent's write fd: other end is open (owned by child) → not in `closed_fds`. No reader in `reads` → not in `pairs`. Parent stays `WaitForWrite`.
   - Child's write fd: other end is open (owned by parent) → not in `closed_fds`. No reader → not in `pairs`. Child stays `WaitForWrite`.
   - No progress made.
5. `iterate_prepare_machine()` finds zero `VmState::Runnable` VMs → returns `Error::Unexpected("A deadlock situation has been reached!")`.
6. `run()` propagates the error → `map_vm_internal_error()` wraps it as `ScriptError::VMInternalError`.
7. Transaction is rejected. Any cell whose lock script is this program is permanently unspendable. [7](#0-6) [8](#0-7)

### Citations

**File:** script/src/types.rs (L336-353)
```rust
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum VmState {
    /// Runnable.
    Runnable,
    /// Terminated.
    Terminated,
    /// Wait.
    Wait {
        /// Target vm id.
        target_vm_id: VmId,
        /// Exit code addr.
        exit_code_addr: u64,
    },
    /// WaitForWrite.
    WaitForWrite(WriteState),
    /// WaitForRead.
    WaitForRead(ReadState),
}
```

**File:** script/src/scheduler.rs (L291-305)
```rust
    pub fn run(&mut self, mode: RunMode) -> Result<TerminatedResult, Error> {
        self.boot_root_vm_if_needed()?;

        let (pause, mut limit_cycles) = match mode {
            RunMode::LimitCycles(limit_cycles) => (Pause::new(), limit_cycles),
            RunMode::Pause(pause, limit_cycles) => (pause, limit_cycles),
        };

        while !self.terminated() {
            limit_cycles = self.iterate_outer(&pause, limit_cycles)?.1;
        }
        assert_eq!(self.iteration_cycles, 0);

        self.terminated_result()
    }
```

**File:** script/src/scheduler.rs (L334-349)
```rust
    /// Returns the machine that needs to be executed in the current iterate.
    fn iterate_prepare_machine(&mut self) -> Result<(u64, &mut M), Error> {
        // Find a runnable VM that has the largest ID.
        let vm_id_to_run = self
            .states
            .iter()
            .rev()
            .filter(|(_, state)| matches!(state, VmState::Runnable))
            .map(|(id, _)| *id)
            .next();
        let vm_id_to_run = vm_id_to_run.ok_or_else(|| {
            Error::Unexpected("A deadlock situation has been reached!".to_string())
        })?;
        let (_context, machine) = self.ensure_get_instantiated(&vm_id_to_run)?;
        Ok((vm_id_to_run, machine))
    }
```

**File:** script/src/scheduler.rs (L729-785)
```rust
    fn process_io(&mut self) -> Result<(), Error> {
        let mut reads: HashMap<Fd, (VmId, ReadState)> = HashMap::default();
        let mut closed_fds: Vec<VmId> = Vec::new();

        self.states.iter().for_each(|(vm_id, state)| {
            if let VmState::WaitForRead(inner_state) = state {
                if self.fds.contains_key(&inner_state.fd.other_fd()) {
                    reads.insert(inner_state.fd, (*vm_id, inner_state.clone()));
                } else {
                    closed_fds.push(*vm_id);
                }
            }
        });
        let mut pairs: Vec<(VmId, ReadState, VmId, WriteState)> = Vec::new();
        self.states.iter().for_each(|(vm_id, state)| {
            if let VmState::WaitForWrite(inner_state) = state {
                if self.fds.contains_key(&inner_state.fd.other_fd()) {
                    if let Some((read_vm_id, read_state)) = reads.get(&inner_state.fd.other_fd()) {
                        pairs.push((*read_vm_id, read_state.clone(), *vm_id, inner_state.clone()));
                    }
                } else {
                    closed_fds.push(*vm_id);
                }
            }
        });
        // Finish read / write syscalls for fds that are closed on the other end
        for vm_id in closed_fds {
            match self.states[&vm_id].clone() {
                VmState::WaitForRead(ReadState { length_addr, .. }) => {
                    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                    machine.inner_mut().memory_mut().store64(
                        &Self::u64_to_reg(length_addr),
                        &<M::Inner as CoreMachine>::REG::zero(),
                    )?;
                    machine
                        .inner_mut()
                        .set_register(A0, Self::u8_to_reg(SUCCESS));
                    self.states.insert(vm_id, VmState::Runnable);
                }
                VmState::WaitForWrite(WriteState {
                    consumed,
                    length_addr,
                    ..
                }) => {
                    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                    machine
                        .inner_mut()
                        .memory_mut()
                        .store64(&Self::u64_to_reg(length_addr), &Self::u64_to_reg(consumed))?;
                    machine
                        .inner_mut()
                        .set_register(A0, Self::u8_to_reg(SUCCESS));
                    self.states.insert(vm_id, VmState::Runnable);
                }
                _ => (),
            }
        }
```

**File:** script/testdata/spawn_cases.c (L60-87)
```c
int parent_write_dead_lock(uint64_t* pid) {
    int err = 0;
    const char* argv[] = {"", 0};
    uint64_t fds[2] = {0};
    err = full_spawn(0, 1, argv, fds, pid);
    CHECK(err);
    uint8_t data[10];
    size_t data_length = sizeof(data);
    err = ckb_write(fds[CKB_STDOUT], data, &data_length);
    CHECK(err);

exit:
    return err;
}

int child_write_dead_lock() {
    int err = 0;
    uint64_t inherited_fds[3] = {0};
    size_t inherited_fds_length = 3;
    err = ckb_inherited_fds(inherited_fds, &inherited_fds_length);
    CHECK(err);
    uint8_t data[10];
    size_t data_length = sizeof(data);
    err = ckb_write(inherited_fds[CKB_STDOUT], data, &data_length);
    CHECK(err);
exit:
    return err;
}
```

**File:** script/src/verify.rs (L566-572)
```rust
    fn map_vm_internal_error(&self, error: VMInternalError, max_cycles: Cycle) -> ScriptError {
        match error {
            VMInternalError::CyclesExceeded => ScriptError::ExceededMaximumCycles(max_cycles),
            VMInternalError::External(reason) if reason.eq("stopped") => ScriptError::Interrupts,
            _ => ScriptError::VMInternalError(error),
        }
    }
```
