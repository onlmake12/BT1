### Title
Off-by-One in VM Count Guard Allows Spawning Beyond `MAX_VMS_COUNT` — (`script/src/scheduler.rs`)

### Summary
The `Scheduler` in `script/src/scheduler.rs` uses a strict `>` comparison instead of `>=` when checking whether the number of active VMs has reached `MAX_VMS_COUNT`. This off-by-one allows a script author to spawn one additional VM beyond the defined consensus limit of 16, bypassing the resource cap.

### Finding Description

`MAX_VMS_COUNT` is defined as the maximum number of VMs that may exist simultaneously: [1](#0-0) 

When processing a `Message::Spawn`, the scheduler checks whether the limit has been reached before booting a new VM: [2](#0-1) 

The guard condition is:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
```

Because `>` is used instead of `>=`, when the current total equals exactly `MAX_VMS_COUNT` (16), the condition evaluates to `16 > 16 = false`, the guard is **not** triggered, and `boot_vm` is called: [3](#0-2) 

After the spawn completes, the total VM count becomes `MAX_VMS_COUNT + 1 = 17`, exceeding the intended limit. The correct operator is `>=`.

This is structurally identical to the reported `_validateStep()` bug: a length/count is passed as the upper bound, but zero-indexing (or in this case, the pre-spawn count) is not accounted for, so the boundary condition is off by one.

### Impact Explanation

A script author can create 17 concurrent VMs instead of the intended 16. Each VM carries its own memory pages, register state, and snapshot data. The extra VM:

- Consumes memory beyond what the `MAX_VMS_COUNT` cap was designed to bound.
- Increases context-switching overhead inside `ensure_vms_instantiated`, which suspends/resumes VMs to stay within `MAX_INSTANTIATED_VMS = 4`: [4](#0-3) 

- Bypasses a consensus-level resource limit. While the cycle limit still bounds CPU, memory consumption per VM is not directly cycle-accounted, so the extra VM represents unbounded additional memory allocation relative to the declared limit.

### Likelihood Explanation

Any unprivileged script author who submits a transaction to the CKB network can trigger this. The attacker simply writes a script (lock or type) that uses the `spawn` syscall to create exactly `MAX_VMS_COUNT` child VMs and then issues one additional spawn. The off-by-one causes the 17th spawn to succeed. No special privileges, keys, or majority hashpower are required — only the ability to submit a transaction containing a crafted script.

### Recommendation

Change the comparison operator from `>` to `>=`:

```diff
- if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
+ if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
``` [5](#0-4) 

This ensures that once the total VM count reaches `MAX_VMS_COUNT`, no further spawns are permitted, matching the documented intent of the constant.

### Proof of Concept

A script written in C using the CKB syscall API would:

1. Recursively call `ckb_spawn` on itself, passing a counter argument.
2. Each spawned child increments the counter and spawns another child.
3. When the counter reaches 16 (i.e., 16 VMs already exist including the root), the 17th `ckb_spawn` call is issued.
4. Due to the off-by-one, the scheduler's guard at line 532 evaluates `16 > 16 = false` and proceeds to boot the 17th VM instead of returning `MAX_VMS_SPAWNED`.
5. The script verifies that the 17th spawn returned `SUCCESS` (0) rather than `MAX_VMS_SPAWNED`, confirming the bypass.

The existing test infrastructure in `script/src/verify/tests/ckb_latest/features_since_v2023.rs` (e.g., `check_spawn_index_out_of_bound`) demonstrates the pattern for writing such spawn-based tests. [6](#0-5)

### Citations

**File:** script/src/scheduler.rs (L32-38)
```rust
pub const ROOT_VM_ID: VmId = FIRST_VM_ID;
/// The maximum number of VMs that can be created at the same time.
pub const MAX_VMS_COUNT: u64 = 16;
/// The maximum number of instantiated VMs.
pub const MAX_INSTANTIATED_VMS: usize = 4;
/// The maximum number of fds.
pub const MAX_FDS: u64 = 64;
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

**File:** script/src/scheduler.rs (L900-907)
```rust
    fn ensure_vms_instantiated(&mut self, ids: &[VmId]) -> Result<(), Error> {
        if ids.len() > MAX_INSTANTIATED_VMS {
            return Err(Error::Unexpected(format!(
                "At most {} VMs can be instantiated but {} are requested!",
                MAX_INSTANTIATED_VMS,
                ids.len()
            )));
        }
```
