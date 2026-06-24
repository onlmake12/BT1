Audit Report

## Title
Unrestricted `ckb_wait` Syscall Allows Any Spawned VM to Consume Sibling Exit Codes, Causing Parent Script Failure - (File: `script/src/scheduler.rs`)

## Summary
The `Message::Wait` handler in `script/src/scheduler.rs` performs no check that the calling VM is the recorded parent of the target VM. Any VM can call `ckb_wait` with an arbitrary target ID, consume that target's exit code from `terminated_vms`, and permanently remove it via `retain`. A malicious spawned child can exploit this to steal a sibling's exit code before the legitimate parent retrieves it, causing the parent's subsequent `ckb_wait` to return `WAIT_FAILURE` and the lock script to fail.

## Finding Description

**No parent-child tracking exists in the scheduler.** The `Scheduler` struct fields are confirmed as: `sg_data`, `syscall_generator`, `syscall_context`, `total_cycles`, `iteration_cycles`, `next_vm_id`, `next_fd_slot`, `states`, `fds`, `inherited_fd`, `instantiated`, `suspended`, `terminated_vms`, `root_vm_args`, `message_box` — no parent relationship map exists. [1](#0-0) 

A grep for `parent_id`, `child_vm`, `vm_parents` in `scheduler.rs` returns zero structural matches. The `boot_vm` function at lines 1014–1039 assigns an ID and inserts the VM into `instantiated` and `states` only — no spawner-to-spawnee mapping is recorded. [2](#0-1) 

**The `Message::Wait` handler has no ownership check.** At lines 564–593, when `Message::Wait(vm_id, args)` is processed, the handler checks `terminated_vms.get(&args.target_id)` and `states.contains_key(&args.target_id)` but never verifies that `vm_id` is the parent of `args.target_id`. The `retain` call at line 575 permanently removes the exit code entry for `args.target_id` from the global map, regardless of which VM called `ckb_wait`. [3](#0-2) 

**The `Wait` syscall passes the target ID directly from script-controlled register A0** with no validation before posting `Message::Wait`. [4](#0-3) 

**Largest-ID-first scheduling makes the attack deterministic.** The scheduler always picks the runnable VM with the largest ID by iterating the `BTreeMap` in reverse. A malicious child (always assigned a higher ID than the root) is guaranteed to execute before the root VM. [5](#0-4) 

**The `child_wait_dead_lock` test explicitly demonstrates that a child can call `ckb_wait(0)` (the parent's PID)**, confirming the complete absence of any ownership enforcement. [6](#0-5) 

**Contrast with fd ownership:** The `Message::Spawn` handler checks `self.fds.get(fd) != Some(&vm_id)` before allowing fd transfer, and `Message::FdRead`/`FdWrite` check `self.fds[&args.fd] == vm_id`. No equivalent guard exists for `Message::Wait`. [7](#0-6) [8](#0-7) 

**Exploit flow:**
1. Root (pid=0) spawns Child A (pid=1, trusted) and Child B (pid=2, malicious cell dep).
2. Child B (pid=2) runs first (largest ID). It stays runnable.
3. Child A (pid=1) terminates with exit code 0. Scheduler executes `terminated_vms.insert(1, 0)` at line 364. No VM is in `VmState::Wait { target_vm_id: 1 }`, so no immediate wakeup. Child A is removed from `states`, `instantiated`, `suspended`.
4. Child B (pid=2) calls `ckb_wait(1)`. The handler finds `terminated_vms[1] = 0`, writes it to Child B's memory, marks Child B `Runnable`, and executes `terminated_vms.retain(|id, _| id != &1)` — permanently removing the entry.
5. Root (pid=0) calls `ckb_wait(1)`. `terminated_vms.get(&1)` → `None`. `states.contains_key(&1)` → `false`. Scheduler returns `WAIT_FAILURE` to root.
6. Root VM receives `WAIT_FAILURE`, fails, transaction is rejected. [9](#0-8) 

## Impact Explanation

This is an **incorrect implementation of CKB-VM system script behavior** (High, 10001–15000 points). A malicious cell dep binary can deterministically cause a lock script using the spawn/wait pattern to fail, rejecting otherwise valid transactions. The `terminated_vms` map is shared mutable state with no per-entry access control, directly analogous to isolation bugs where shared framework state is exploitable by any callee. If the lock script uses the child's exit code for authorization decisions, the malicious child can also manipulate authorization outcomes by forcing incorrect return values. [10](#0-9) 

## Likelihood Explanation

The attack requires a lock script that spawns multiple children and allows at least one child's code to be supplied by the transaction author (e.g., a plugin or verifier cell dep pattern). This is a realistic design in modular lock scripts. The scheduler's deterministic largest-ID-first execution order eliminates any timing dependency: the malicious child is always guaranteed to run before the root VM. Sibling PIDs are sequential integers starting from 1 and are trivially predictable. The attack is fully reproducible without any race condition or external coordination. [5](#0-4) 

## Recommendation

Track the parent-child relationship at spawn time. In `boot_vm` (called from the `Message::Spawn` handler at line 539), record `spawner_vm_id → spawned_vm_id` in a new `BTreeMap<VmId, VmId>` field (e.g., `vm_parents`). In the `Message::Wait` handler, before processing the wait, verify that `vm_parents.get(&args.target_id) == Some(&vm_id)`. If the check fails, return `WAIT_FAILURE` immediately without consuming or removing the `terminated_vms` entry. This mirrors the existing fd ownership check pattern used in `Message::Spawn` (line 525), `Message::FdRead` (line 620), and `Message::FdWrite` (line 646). [11](#0-10) 

## Proof of Concept

**Minimal test plan:**

1. Write a root script (pid=0) that spawns two children and calls `ckb_wait` on pid=1, asserting `SUCCESS`.
2. Write Child A (pid=1) that immediately exits with code 0.
3. Write Child B (pid=2, malicious) that calls `ckb_wait(1)` before returning — consuming pid=1's exit code from `terminated_vms`.
4. Construct a CKB transaction with the root script as the lock, Child A from a trusted cell dep, Child B from an attacker-controlled cell dep.
5. Execute the transaction through the scheduler. Observe that root's `ckb_wait(1)` returns `WAIT_FAILURE` and the script fails.

The existing `child_wait_dead_lock` function in `script/testdata/spawn_cases.c` (lines 147–151) already demonstrates that a child can call `ckb_wait` on an arbitrary PID (the parent's pid=0) with no rejection, confirming the absence of any ownership enforcement and providing a direct template for the sibling-targeting variant. [6](#0-5)

### Citations

**File:** script/src/scheduler.rs (L46-122)
```rust
pub struct Scheduler<DL, V, M>
where
    DL: CellDataProvider,
    M: DefaultMachineRunner,
{
    /// Immutable context data for current running transaction & script.
    sg_data: SgData<DL>,

    /// Syscall generator
    syscall_generator: SyscallGenerator<DL, V, M::Inner>,
    /// Syscall generator context
    syscall_context: V,

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
    /// Next vm id used by spawn.
    next_vm_id: VmId,
    /// Next fd used by pipe.
    next_fd_slot: u64,
    /// Used to store VM state.
    states: BTreeMap<VmId, VmState>,
    /// Used to confirm the owner of fd.
    fds: BTreeMap<Fd, VmId>,
    /// Verify the VM's inherited fd list.
    inherited_fd: BTreeMap<VmId, Vec<Fd>>,
    /// Instantiated vms.
    instantiated: BTreeMap<VmId, (VmContext<DL>, M)>,
    /// Suspended vms.
    suspended: BTreeMap<VmId, Snapshot2<DataPieceId>>,
    /// Terminated vms.
    terminated_vms: BTreeMap<VmId, i8>,
    /// Root vm's arguments. Provided for compatibility with surrounding tools. You should not
    /// read it anywhere except when initializing the root vm.
    /// Note: This field is intentionally not serialized in FullSuspendedState.
    root_vm_args: Vec<Bytes>,

    /// MessageBox is expected to be empty before returning from `run`
    /// function, there is no need to persist messages.
    message_box: Arc<Mutex<Vec<Message>>>,
}
```

**File:** script/src/scheduler.rs (L336-348)
```rust
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
```

**File:** script/src/scheduler.rs (L362-405)
```rust
        match result {
            Ok(code) => {
                self.terminated_vms.insert(vm_id_to_run, code);
                // When root VM terminates, the execution stops immediately, we will purge
                // all non-root VMs, and only keep root VM in states.
                // When non-root VM terminates, we only purge the VM's own states.
                if vm_id_to_run == ROOT_VM_ID {
                    self.ensure_vms_instantiated(&[vm_id_to_run])?;
                    self.instantiated.retain(|id, _| *id == vm_id_to_run);
                    self.suspended.clear();
                    self.states.clear();
                    self.states.insert(vm_id_to_run, VmState::Terminated);
                } else {
                    let joining_vms: Vec<(VmId, u64)> = self
                        .states
                        .iter()
                        .filter_map(|(vm_id, state)| match state {
                            VmState::Wait {
                                target_vm_id,
                                exit_code_addr,
                            } if *target_vm_id == vm_id_to_run => Some((*vm_id, *exit_code_addr)),
                            _ => None,
                        })
                        .collect();
                    // For all joining VMs, update exit code, then mark them as
                    // runnable state.
                    for (vm_id, exit_code_addr) in joining_vms {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .memory_mut()
                            .store8(&Self::u64_to_reg(exit_code_addr), &Self::i8_to_reg(code))?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(SUCCESS));
                        self.states.insert(vm_id, VmState::Runnable);
                    }
                    // Close fds
                    self.fds.retain(|_, vm_id| *vm_id != vm_id_to_run);
                    // Clear terminated VM states
                    self.states.remove(&vm_id_to_run);
                    self.instantiated.remove(&vm_id_to_run);
                    self.suspended.remove(&vm_id_to_run);
                }
```

**File:** script/src/scheduler.rs (L525-525)
```rust
                    if args.fds.iter().any(|fd| self.fds.get(fd) != Some(&vm_id)) {
```

**File:** script/src/scheduler.rs (L539-562)
```rust
                    let spawned_vm_id = self.boot_vm(
                        &args.location,
                        VmArgs::Reader {
                            vm_id,
                            argc: args.argc,
                            argv: args.argv,
                        },
                    )?;
                    // Move passed fds from spawner to spawnee
                    for fd in &args.fds {
                        self.fds.insert(*fd, spawned_vm_id);
                    }
                    // Here we keep the original version of file descriptors.
                    // If one fd is moved afterward, this inherited file descriptors doesn't change.
                    self.inherited_fd.insert(spawned_vm_id, args.fds.clone());

                    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                    machine.inner_mut().memory_mut().store64(
                        &Self::u64_to_reg(args.process_id_addr),
                        &Self::u64_to_reg(spawned_vm_id),
                    )?;
                    machine
                        .inner_mut()
                        .set_register(A0, Self::u8_to_reg(SUCCESS));
```

**File:** script/src/scheduler.rs (L564-593)
```rust
                Message::Wait(vm_id, args) => {
                    if let Some(exit_code) = self.terminated_vms.get(&args.target_id).copied() {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine.inner_mut().memory_mut().store8(
                            &Self::u64_to_reg(args.exit_code_addr),
                            &Self::i8_to_reg(exit_code),
                        )?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(SUCCESS));
                        self.states.insert(vm_id, VmState::Runnable);
                        self.terminated_vms.retain(|id, _| id != &args.target_id);
                        continue;
                    }
                    if !self.states.contains_key(&args.target_id) {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(WAIT_FAILURE));
                        continue;
                    }
                    // Return code will be updated when the joining VM exits
                    self.states.insert(
                        vm_id,
                        VmState::Wait {
                            target_vm_id: args.target_id,
                            exit_code_addr: args.exit_code_addr,
                        },
                    );
                }
```

**File:** script/src/scheduler.rs (L619-626)
```rust
                Message::FdRead(vm_id, args) => {
                    if !(self.fds.contains_key(&args.fd) && (self.fds[&args.fd] == vm_id)) {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(INVALID_FD));
                        continue;
                    }
```

**File:** script/src/scheduler.rs (L1014-1039)
```rust
    /// Boot a vm by given program and args.
    fn boot_vm(&mut self, location: &DataLocation, args: VmArgs) -> Result<VmId, Error> {
        let id = self.next_vm_id;
        self.next_vm_id += 1;
        let (context, mut machine) = self.create_dummy_vm(&id)?;
        let (program, _) = {
            let sc = context.snapshot2_context.lock().expect("lock");
            sc.load_data(&location.data_piece_id, location.offset, location.length)?
        };
        self.load_vm_program(&context, &mut machine, location, program, args)?;
        // Newly booted VM will be instantiated by default
        while self.instantiated.len() >= MAX_INSTANTIATED_VMS {
            // Instantiated is a BTreeMap, first_entry will maintain key order
            let id = *self
                .instantiated
                .first_entry()
                .ok_or_else(|| Error::Unexpected("Map should not be empty".to_string()))?
                .key();
            self.suspend_vm(&id)?;
        }

        self.instantiated.insert(id, (context, machine));
        self.states.insert(id, VmState::Runnable);

        Ok(id)
    }
```

**File:** script/src/syscalls/wait.rs (L37-49)
```rust
        let target_id = machine.registers()[A0].to_u64();
        let exit_code_addr = machine.registers()[A1].to_u64();
        machine.add_cycles_no_checking(SPAWN_YIELD_CYCLES_BASE)?;
        self.message_box
            .lock()
            .map_err(|e| VMError::Unexpected(e.to_string()))?
            .push(Message::Wait(
                self.id,
                WaitArgs {
                    target_id,
                    exit_code_addr,
                },
            ));
```

**File:** script/testdata/spawn_cases.c (L147-151)
```c
int child_wait_dead_lock() {
    uint64_t pid = 0;  // parent pid
    int8_t exit_code = 0;
    return ckb_wait(pid, &exit_code);
}
```
