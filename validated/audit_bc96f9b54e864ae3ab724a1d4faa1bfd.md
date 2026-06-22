### Title
Off-by-One in Spawn VM Count Guard Allows 17th VM Creation — (`script/src/scheduler.rs`)

### Summary

The `Message::Spawn` handler in `process_message_box` uses a strict `>` comparison instead of `>=` when checking whether the active VM count has reached `MAX_VMS_COUNT` (16). This allows an unprivileged script author to spawn exactly one extra VM beyond the intended limit.

### Finding Description

The guard at line 532 reads:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
``` [1](#0-0) 

`MAX_VMS_COUNT` is defined as `16`: [2](#0-1) 

When exactly 16 VMs are active (`suspended.len() + instantiated.len() == 16`), the condition evaluates to `16 > 16 = false`, so the guard does **not** fire. `boot_vm` is then called unconditionally: [3](#0-2) 

`boot_vm` inserts the new VM into `self.instantiated` and `self.states` without any secondary count check: [4](#0-3) 

The result is 17 concurrently active VMs, violating the invariant that at most `MAX_VMS_COUNT` VMs exist at any time.

The correct guard should be `>=`:

```rust
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

### Impact Explanation

1. **Invariant violation**: The 17th VM is created and runs. Any downstream logic that assumes `suspended + instantiated <= 16` (e.g., serialization, snapshot bounds, `ensure_vms_instantiated` assertions) operates on an unexpected state.

2. **Consensus deviation**: If a patched build (using `>=`) is deployed to any subset of nodes without a coordinated hardfork, those nodes will reject transactions that spawn exactly 16 child VMs (17 total including root), while unpatched nodes accept them. This creates a chain split on any transaction exercising the boundary.

3. **Resource exhaustion (bounded)**: The attacker gains one extra VM per script execution. Cycle limits still apply, so this is not unbounded — but it does exceed the documented and enforced resource cap.

### Likelihood Explanation

The exploit path is fully unprivileged and requires only a crafted lock/type script submitted in a normal transaction. No special roles, keys, or network position are needed. The script simply spawns 15 child VMs (16 total with root), then issues one more `Spawn` syscall. The 17th VM boots and runs. This is locally testable against the current codebase.

### Recommendation

Change the comparison operator from `>` to `>=` at line 532:

```rust
// Before (buggy):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
``` [5](#0-4) 

Deploy the fix via a coordinated hardfork or consensus-version gate to avoid the chain-split scenario described above.

### Proof of Concept

1. Write a lock script that calls `Spawn` in a loop, spawning child VMs that each block on a pipe read (keeping them alive and counted).
2. After 15 successful spawns (16 total VMs including root), issue one more `Spawn`.
3. Observe: the 16th child (17th VM total) is created and returns `SUCCESS` (register `A0 = 0`).
4. Differential test: compile the same script against a build with `>=`; the 16th child spawn returns `MAX_VMS_SPAWNED` instead.

The divergence in return code between the two builds is the concrete, locally reproducible proof of the off-by-one.

### Citations

**File:** script/src/scheduler.rs (L34-34)
```rust
pub const MAX_VMS_COUNT: u64 = 16;
```

**File:** script/src/scheduler.rs (L532-538)
```rust
                    if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_VMS_SPAWNED));
                        continue;
                    }
```

**File:** script/src/scheduler.rs (L539-546)
```rust
                    let spawned_vm_id = self.boot_vm(
                        &args.location,
                        VmArgs::Reader {
                            vm_id,
                            argc: args.argc,
                            argv: args.argv,
                        },
                    )?;
```

**File:** script/src/scheduler.rs (L1035-1036)
```rust
        self.instantiated.insert(id, (context, machine));
        self.states.insert(id, VmState::Runnable);
```
