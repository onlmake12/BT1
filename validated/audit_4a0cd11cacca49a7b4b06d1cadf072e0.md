Audit Report

## Title
Off-by-One in VM Count Guard Allows 17 Concurrent VMs Instead of Documented 16 — (`script/src/scheduler.rs`)

## Summary
`MAX_VMS_COUNT` is defined as 16 and documented as "The maximum number of VMs that can be created at the same time," but the spawn guard uses a strict `>` comparison, allowing the total VM count to reach 17 before rejecting. This is a concrete divergence between the documented invariant and the enforced behavior of the CKB-VM scheduler.

## Finding Description
`MAX_VMS_COUNT` is set to 16 at [1](#0-0)  with the doc-comment "The maximum number of VMs that can be created at the same time."

The guard in `Scheduler::process_message_box` at the `Message::Spawn` branch is:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
``` [2](#0-1) 

This check runs **before** `boot_vm` is called (line 539), so it evaluates the count of already-existing VMs. When the root VM (always present in `instantiated`) has spawned 15 children, the count is 16. Since `16 > 16` is `false`, the 16th spawn succeeds, pushing the total to 17. Only the 17th spawn attempt (count = 17, `17 > 16` = `true`) is rejected.

The test file `spawn_create_17_spawn.c` encodes exactly this behavior: `SPAWN_TIMES = 17`, and the error branch only triggers when `i >= 16`, meaning spawns at indices 0–15 (16 children) are expected to succeed. [3](#0-2) [4](#0-3) 

The correct predicate is `>= MAX_VMS_COUNT as usize`, which rejects the spawn when the count is already at 16, keeping the total at or below 16.

## Impact Explanation
This is a concrete incorrect behavior of the CKB-VM scheduler: the enforced VM limit (17) diverges from the value and documentation of `MAX_VMS_COUNT` (16). This falls squarely under the allowed impact class **"Incorrect implementation or behavior of CKB-VM or system scripts"** (High, 10001–15000 points). The over-count is bounded at +1 and does not cause crashes or consensus splits between nodes (all nodes run the same code), but it constitutes a verifiable violation of a documented invariant in the VM execution layer.

## Likelihood Explanation
Any V2 script author can trigger this by calling `ckb_spawn` in a loop. No privileged access, key material, or special conditions are required. The 16th spawn call (which should be rejected per the documented limit) succeeds unconditionally with the current code.

## Recommendation
Change the comparison operator from strict-greater-than to greater-than-or-equal in `script/src/scheduler.rs` line 532:

```rust
// Before (off-by-one):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

The companion test `spawn_create_17_spawn.c` must also be updated: `SPAWN_TIMES` should be reduced to 16, and the boundary check should expect failure at `i >= 15` (the 16th spawn attempt), not `i >= 16`.

## Proof of Concept
The existing test `script/testdata/spawn_create_17_spawn.c` already encodes the scenario. With the current code:
1. The root VM loops calling `ckb_spawn` 17 times.
2. Spawns at indices 0–15 (16 children) return `CKB_SUCCESS`, making total VM count = 17.
3. Only spawn at index 16 returns `CKB_MAX_VMS_SPAWNED`.

This directly contradicts `MAX_VMS_COUNT = 16`. Running the existing test suite against the current scheduler confirms the off-by-one is live, as the test passes with `SPAWN_TIMES = 17` and `i < 16` as the success boundary. [5](#0-4)

### Citations

**File:** script/src/scheduler.rs (L33-34)
```rust
/// The maximum number of VMs that can be created at the same time.
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

**File:** script/testdata/spawn_create_17_spawn.c (L6-6)
```c
#define SPAWN_TIMES 17
```

**File:** script/testdata/spawn_create_17_spawn.c (L100-106)
```c
        } else {
            printf("invoke spawn: %d err: %d\n", i, ret);
            if ((i < 16) || (ret != CKB_MAX_VMS_SPAWNED)) {
                printf("Unexpected spawn error!\n");
                return -1;
            }
        }
```
