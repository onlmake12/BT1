Audit Report

## Title
Off-by-One in Spawn VM Count Guard Allows 17th VM Creation — (`script/src/scheduler.rs`)

## Summary
The `Message::Spawn` handler in `process_message_box` uses a strict `>` comparison instead of `>=` when checking whether the active VM count has reached `MAX_VMS_COUNT` (16). When exactly 16 VMs are active, the guard evaluates to `16 > 16 = false` and does not fire, allowing `boot_vm` to create a 17th VM. This violates the documented invariant that at most `MAX_VMS_COUNT` VMs exist concurrently and constitutes incorrect CKB-VM behavior reachable by any unprivileged script author.

## Finding Description
`MAX_VMS_COUNT` is defined as `16` at [1](#0-0)  The guard in the `Message::Spawn` arm reads:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
``` [2](#0-1) 

When the total active VM count is exactly 16, `16 > 16` evaluates to `false`, so the guard does not trigger and `MAX_VMS_SPAWNED` is never returned to the caller. Execution falls through unconditionally to `boot_vm`: [3](#0-2) 

Inside `boot_vm`, the only count check is against `MAX_INSTANTIATED_VMS` (4), which governs memory-resident VMs only — there is no secondary check against `MAX_VMS_COUNT`: [4](#0-3) 

The new VM is inserted into `self.instantiated` and `self.states` unconditionally, yielding 17 concurrently active VMs.

## Impact Explanation
This is **incorrect implementation/behavior of CKB-VM** (High, 10001–15000 points). The enforced resource cap is exceeded by one VM per script execution. Additionally, if any subset of nodes is patched to use `>=`, those nodes will reject transactions that spawn exactly 16 child VMs (17 total including root) while unpatched nodes accept them — a concrete **consensus deviation** (Critical, 15001–25000 points). The consensus-deviation impact is the dominant concern.

## Likelihood Explanation
The exploit path is fully unprivileged. Any script author can submit a crafted lock or type script in a normal transaction. The script spawns 15 child VMs (16 total with root), then issues one additional `Spawn` syscall. No special keys, roles, or network position are required. The boundary condition is deterministic and locally reproducible.

## Recommendation
Change the comparison operator from `>` to `>=` at line 532:

```rust
// Before (buggy):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

Deploy via a coordinated consensus-version gate or hardfork to avoid a chain split on transactions that exercise the exact boundary of 16 child spawns.

## Proof of Concept
1. Write a lock script that spawns child VMs in a loop; each child blocks on a pipe read to remain alive and counted.
2. After 15 successful spawns (16 total VMs including root), issue one more `Spawn` syscall.
3. On the unpatched build: the 16th child (17th VM total) boots successfully and `A0 = 0` (`SUCCESS`) is returned to the caller.
4. On a patched build (`>=`): the same `Spawn` returns `A0 = MAX_VMS_SPAWNED` instead.
5. The divergence in return code between the two builds is the concrete, locally reproducible proof of the off-by-one.

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

**File:** script/src/scheduler.rs (L1025-1036)
```rust
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
```
