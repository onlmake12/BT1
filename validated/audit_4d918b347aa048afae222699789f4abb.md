The code confirms the claim. At line 532, the check uses `>` (strict greater than):

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
```

With `MAX_VMS_COUNT = 16`, when exactly 16 VMs are alive, `16 > 16` is `false` and the spawn proceeds, creating a 17th VM. The `MAX_FDS` check at line 595 correctly uses `>=`. The inconsistency is real and confirmed in the actual code.

---

Audit Report

## Title
Off-by-One in Spawn VM Count Guard Allows Exceeding `MAX_VMS_COUNT` - (File: script/src/scheduler.rs)

## Summary
The `Scheduler` in `script/src/scheduler.rs` uses a strict `>` comparison instead of `>=` when checking whether the number of alive VMs has reached `MAX_VMS_COUNT` (16) before permitting a `spawn` syscall. When exactly 16 VMs are alive, the condition `16 > 16` evaluates to `false`, so `boot_vm` is called and a 17th VM is created. The analogous `MAX_FDS` guard on the same file correctly uses `>=`, confirming the `MAX_VMS_COUNT` check is erroneous.

## Finding Description
In `process_message_box`, the `Message::Spawn` handler at line 532 of `script/src/scheduler.rs` reads:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
    // return MAX_VMS_SPAWNED
}
```

`MAX_VMS_COUNT` is defined as `16` (line 34). The guard is intended to reject a spawn when the alive VM count is already at the limit. However, because `>` is used instead of `>=`, the condition is only `true` when the count is **17 or more**, not when it is exactly 16. Consequently, when `suspended.len() + instantiated.len() == 16`, the guard passes and `boot_vm` (line 539) inserts a 17th entry into `self.instantiated` / `self.suspended` and `self.states`.

By contrast, the `Message::Pipe` handler at line 595 uses the correct operator:

```rust
if self.fds.len() as u64 >= MAX_FDS {
    // return MAX_FDS_CREATED
}
```

The inconsistency is structural: both guards are meant to enforce an upper bound on a resource count, but only the `MAX_FDS` guard is correct.

## Impact Explanation
This is an **incorrect implementation of CKB-VM behavior** — a High-severity impact class (10001–15000 points) under the allowed CKB bounty scope. The protocol explicitly defines `MAX_VMS_COUNT = 16` as the maximum number of concurrent VMs a script may create. The implementation silently violates this invariant, allowing any script to hold 17 live VMs. Because all nodes execute the same flawed code, there is no consensus split, but the CKB-VM resource accounting is incorrect: the scheduler's `states`, `instantiated`/`suspended` maps, and associated memory all grow beyond the protocol-defined ceiling. This constitutes a concrete incorrect behavior of the CKB-VM implementation.

## Likelihood Explanation
Any unprivileged transaction sender can trigger this. No special permissions, keys, or external conditions are required. A script that spawns 15 children (root + 15 = 16 alive VMs) and then issues one additional `ckb_spawn` syscall will receive `SUCCESS` instead of `MAX_VMS_SPAWNED`. The path is a standard lock or type script submitted in a transaction. The condition is deterministic and 100% reproducible.

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
5. The existing test `check_spawn_max_vms_count` (line 103, `features_since_v2023.rs`) passes argument `10` to `spawn_cases` but does not assert the exact boundary of 16 alive VMs, leaving the off-by-one undetected. A targeted test that asserts `MAX_VMS_SPAWNED` is returned when exactly 16 VMs are alive would catch this regression.