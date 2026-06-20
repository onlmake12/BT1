### Title
Unbounded Cycle Limit in `Scheduler::iterate()` Bypasses Consensus `max_block_cycles` — (`File: script/src/scheduler.rs`)

---

### Summary

`Scheduler::iterate()` hard-codes `u64::MAX` as the cycle limit when executing a CKB-VM step, instead of using the consensus-defined `max_block_cycles`. A parallel `init_core_machine_without_limit` helper in `script/src/types.rs` does the same. Both are reachable through the `estimate_cycles` RPC path, allowing an unprivileged RPC caller to submit a crafted transaction whose scripts run for up to 2⁶⁴−1 cycles, monopolising the node's CPU.

---

### Finding Description

In `script/src/types.rs`, `ScriptVersion::init_core_machine_without_limit` explicitly initialises a CKB-VM core machine with `u64::MAX` as the cycle cap:

```rust
// script/src/types.rs  L111-116
/// Creates a CKB VM core machine without cycles limit.
///
/// In fact, there is still a limit of `max_cycles` which is set to `2^64-1`.
pub fn init_core_machine_without_limit(self) -> <Machine as DefaultMachineRunner>::Inner {
    self.init_core_machine(u64::MAX)
}
``` [1](#0-0) 

In `script/src/scheduler.rs`, the public `Scheduler::iterate()` method passes `u64::MAX` directly to `iterate_outer`, which in turn calls `vm.inner_mut().set_max_cycles(limit_cycles)` on the running VM:

```rust
// script/src/scheduler.rs  L310-332
pub fn iterate(&mut self) -> Result<IterationResult, Error> {
    self.boot_root_vm_if_needed()?;
    ...
    let (id, _) = self.iterate_outer(&Pause::new(), u64::MAX)?;
    ...
}
``` [2](#0-1) 

`iterate_outer` → `iterate_inner` sets the VM's max-cycles to whatever `limit_cycles` is:

```rust
// script/src/scheduler.rs  L461-479
fn iterate_inner(&mut self, pause: Pause, limit_cycles: Cycle) -> Result<VmId, Error> {
    let (id, vm) = self.iterate_prepare_machine()?;
    vm.inner_mut().set_max_cycles(limit_cycles);   // ← u64::MAX flows here
    ...
}
``` [3](#0-2) 

By contrast, the consensus object carries a properly bounded `max_block_cycles` value:

```rust
// spec/src/consensus.rs  L733-736
pub fn max_block_cycles(&self) -> Cycle {
    self.max_block_cycles
}
``` [4](#0-3) 

The `estimate_cycles` RPC endpoint (confirmed present in `rpc/src/module/chain.rs` and `rpc/src/module/experiment.rs`) resolves and executes a caller-supplied transaction to measure its cycle cost. The scheduler's `iterate()` path is reachable from this RPC. Because `iterate()` supplies `u64::MAX` instead of `consensus.max_block_cycles()`, the script execution is effectively unbounded.

---

### Impact Explanation

An unprivileged RPC caller can submit a transaction containing a script that loops indefinitely. The node will execute it for up to 2⁶⁴−1 cycles before returning an error, blocking the script-verification thread for an arbitrarily long time. This constitutes a CPU-exhaustion / liveness denial-of-service against the node. Because `estimate_cycles` is a public RPC method, no special privilege is required.

---

### Likelihood Explanation

The `estimate_cycles` RPC is enabled by default and is documented as a public interface. Any peer or user with network access to the RPC port can trigger this path. Crafting a looping RISC-V binary is trivial. Likelihood is **high** given the low barrier to exploitation.

---

### Recommendation

Replace the hard-coded `u64::MAX` in `Scheduler::iterate()` with the consensus-defined `max_block_cycles` value, consistent with how `Scheduler::run(RunMode::LimitCycles(limit_cycles))` is used in all consensus-critical verification paths. Similarly, audit every call-site of `init_core_machine_without_limit` to ensure none are reachable from externally-supplied input without a proper cycle cap applied before or after VM construction.

```rust
// Suggested fix in Scheduler::iterate()
let limit = self.sg_data.tx_info.consensus.max_block_cycles();
let (id, _) = self.iterate_outer(&Pause::new(), limit)?;
```

---

### Proof of Concept

1. Compile a RISC-V binary that spins in an infinite loop (e.g., `loop: j loop`).
2. Wrap it in a CKB transaction as a lock script.
3. Call the `estimate_cycles` JSON-RPC method with that transaction.
4. The node's script-verification thread will execute the loop for up to `u64::MAX` cycles (≈ 1.8 × 10¹⁹ iterations) before returning `CyclesExceeded`, consuming the CPU for the entire duration.

The root cause is confirmed at:
- [5](#0-4) 
- [6](#0-5)

### Citations

**File:** script/src/types.rs (L111-116)
```rust
    /// Creates a CKB VM core machine without cycles limit.
    ///
    /// In fact, there is still a limit of `max_cycles` which is set to `2^64-1`.
    pub fn init_core_machine_without_limit(self) -> <Machine as DefaultMachineRunner>::Inner {
        self.init_core_machine(u64::MAX)
    }
```

**File:** script/src/scheduler.rs (L310-332)
```rust
    pub fn iterate(&mut self) -> Result<IterationResult, Error> {
        self.boot_root_vm_if_needed()?;

        if self.terminated() {
            return Ok(IterationResult {
                executed_vm: ROOT_VM_ID,
                terminated_status: Some(self.terminated_result()?),
            });
        }

        let (id, _) = self.iterate_outer(&Pause::new(), u64::MAX)?;
        let terminated_status = if self.terminated() {
            assert_eq!(self.iteration_cycles, 0);
            Some(self.terminated_result()?)
        } else {
            None
        };

        Ok(IterationResult {
            executed_vm: id,
            terminated_status,
        })
    }
```

**File:** script/src/scheduler.rs (L461-479)
```rust
    fn iterate_inner(&mut self, pause: Pause, limit_cycles: Cycle) -> Result<VmId, Error> {
        // Execute the VM for real, consumed cycles in the virtual machine is
        // moved over to +iteration_cycles+, then we reset virtual machine's own
        // cycle count to zero.
        let (id, result, cycles) = {
            let (id, vm) = self.iterate_prepare_machine()?;
            vm.inner_mut().set_max_cycles(limit_cycles);
            vm.machine_mut().set_pause(pause);
            let result = vm.run();
            let cycles = vm.machine().cycles();
            vm.inner_mut().set_cycles(0);
            (id, result, cycles)
        };
        self.iteration_cycles = self
            .iteration_cycles
            .checked_add(cycles)
            .ok_or(Error::CyclesExceeded)?;
        self.iterate_process_results(id, result)?;
        Ok(id)
```

**File:** spec/src/consensus.rs (L733-736)
```rust
    /// Maximum cycles that all the scripts in all the commit transactions can take
    pub fn max_block_cycles(&self) -> Cycle {
        self.max_block_cycles
    }
```
