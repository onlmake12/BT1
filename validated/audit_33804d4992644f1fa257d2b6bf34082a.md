The code confirms the claim exactly as described.

- `MAX_VMS_COUNT = 16` at line 34. [1](#0-0) 
- Line 532 uses `>` (strict greater than): `if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize` — when exactly 16 VMs are alive, `16 > 16` is `false`, the guard is skipped, and `boot_vm` at line 539 creates a 17th VM. [2](#0-1) 
- Line 595 uses `>=` for the analogous `MAX_FDS` guard, confirming the inconsistency. [3](#0-2) 
- The test `check_spawn_max_vms_count` at line 103 passes argument `10` to `spawn_cases` and does not assert the exact 16-VM boundary. [4](#0-3) 

---

Audit Report

## Title
Off-by-One in Spawn VM Count Guard Allows Exceeding `MAX_VMS_COUNT` - (File: script/src/scheduler.rs)

## Summary
The `Scheduler` in `script/src/scheduler.rs` uses `>` instead of `>=` when checking whether the alive VM count has reached `MAX_VMS_COUNT` (16) before permitting a `spawn` syscall. When exactly 16 VMs are alive, `16 > 16` is `false`, so `boot_vm` is called and a 17th VM is created. The analogous `MAX_FDS` guard on the same file correctly uses `>=`, confirming the `MAX_VMS_COUNT` check is erroneous.

## Finding Description
In `process_message_box`, the `Message::Spawn` handler at line 532 of `script/src/scheduler.rs` reads:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
    // return MAX_VMS_SPAWNED
}
```

`MAX_VMS_COUNT` is defined as `16` (line 34). The guard is intended to reject a spawn when the alive VM count is already at the limit. However, because `>` is used instead of `>=`, the condition is only `true` when the count is **17 or more**, not when it is exactly 16. Consequently, when `suspended.len() + instantiated.len() == 16`, the guard passes and `boot_vm` (line 539) inserts a 17th entry into `self.instantiated`/`self.suspended` and `self.states`.

By contrast, the `Message::Pipe` handler at line 595 uses the correct operator:

```rust
if self.fds.len() as u64 >= MAX_FDS {
    // return MAX_FDS_CREATED
}
```

Both guards are structurally identical resource-cap checks; only the `MAX_FDS` guard is correct. The existing test `check_spawn_max_vms_count` (line 103, `features_since_v2023.rs`) passes argument `10` to `spawn_cases` and does not assert the exact 16-VM boundary, leaving the off-by-one undetected.

## Impact Explanation
This is an incorrect implementation of CKB-VM behavior — a **High** severity impact (10001–15000 points) under the allowed CKB bounty scope. The protocol explicitly defines `MAX_VMS_COUNT = 16` as the maximum number of concurrent VMs a script may create. The implementation silently violates this invariant, allowing any script to hold 17 live VMs. Because all nodes execute the same flawed code there is no consensus split, but the CKB-VM resource accounting is incorrect: the scheduler's `states`, `instantiated`/`suspended` maps, and associated memory all grow beyond the protocol-defined ceiling. This constitutes a concrete incorrect behavior of the CKB-VM implementation.

## Likelihood Explanation
Any unprivileged transaction sender can trigger this. No special permissions, keys, or external conditions are required. A script that spawns 15 children (root + 15 = 16 alive VMs) and then issues one additional `ckb_spawn` syscall will receive `SUCCESS` instead of `MAX_VMS_SPAWNED`. The condition is deterministic and 100% reproducible.

## Recommendation
Change the comparison operator from `>` to `>=` at line 532 of `script/src/scheduler.rs`:

```diff
- if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
+ if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

This makes the guard consistent with the `MAX_FDS` check and correctly rejects a spawn when the alive VM count is already at the protocol limit of 16.

## Proof of Concept
1. Write a CKB script (lock or type) that uses the `ckb_spawn` syscall recursively: the root VM spawns a child, which spawns a child, and so on, until 15 children are alive (total: root + 15 = 16 VMs).
2. Any alive VM then calls `ckb_spawn` one more time.
3. **Current behavior (`>`):** `suspended.len() + instantiated.len()` is 16; `16 > 16` is `false`; the guard is skipped; `boot_vm` creates VM 17; the syscall returns `SUCCESS`.
4. **Expected behavior (`>=`):** `16 >= 16` is `true`; the guard fires; the syscall returns `MAX_VMS_SPAWNED`.
5. A targeted unit test that asserts `MAX_VMS_SPAWNED` is returned when exactly 16 VMs are alive would catch this regression; the existing `check_spawn_max_vms_count` test does not cover this boundary.

### Citations

**File:** script/src/scheduler.rs (L34-34)
```rust
pub const MAX_VMS_COUNT: u64 = 16;
```

**File:** script/src/scheduler.rs (L532-539)
```rust
                    if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
                        let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
                        machine
                            .inner_mut()
                            .set_register(A0, Self::u8_to_reg(MAX_VMS_SPAWNED));
                        continue;
                    }
                    let spawned_vm_id = self.boot_vm(
```

**File:** script/src/scheduler.rs (L595-595)
```rust
                    if self.fds.len() as u64 >= MAX_FDS {
```

**File:** script/src/verify/tests/ckb_latest/features_since_v2023.rs (L103-106)
```rust
fn check_spawn_max_vms_count() {
    let result = simple_spawn_test("testdata/spawn_cases", &[10]);
    assert_eq!(result.is_ok(), SCRIPT_VERSION == ScriptVersion::V2);
}
```
