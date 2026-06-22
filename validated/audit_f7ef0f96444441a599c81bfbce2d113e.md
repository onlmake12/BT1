### Title
Off-by-One in VM Count Guard Allows 17 Concurrent VMs Instead of Documented 16 — (`script/src/scheduler.rs`)

---

### Summary

`Scheduler::process_message_box` uses a strict-greater-than comparison (`>`) against `MAX_VMS_COUNT` (16) when deciding whether to reject a `Spawn` syscall. Because the check fires only *after* the count already exceeds 16, a script can successfully spawn a 17th VM, violating the invariant stated in the constant's own doc-comment.

---

### Finding Description

`MAX_VMS_COUNT` is documented as *"The maximum number of VMs that can be created at the same time"* and is set to 16. [1](#0-0) 

The guard in the `Message::Spawn` branch is:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
    // return MAX_VMS_SPAWNED error
}
``` [2](#0-1) 

The root VM is always present in `instantiated` when it issues a Spawn. Tracing the count:

| Spawn attempt | Count before check | `count > 16`? | Result |
|---|---|---|---|
| 1st child | 1 | false | success → total = 2 |
| … | … | false | … |
| 15th child | 15 | false | success → total = 16 |
| **16th child** | **16** | **false** | **success → total = 17** |
| 17th child | 17 | true | rejected |

The correct predicate is `>= MAX_VMS_COUNT as usize`, which would reject the spawn attempt when the count is already at the limit (16), keeping the total at 16.

The testdata file `spawn_create_17_spawn.c` (with `SPAWN_TIMES = 17`) confirms this: it expects spawns 0–15 (16 children) to succeed and only spawn index 16 to return `CKB_MAX_VMS_SPAWNED`, which is exactly the off-by-one behavior. [3](#0-2) [4](#0-3) 

---

### Impact Explanation

Any unprivileged V2 script author can craft a lock/type script that calls `ckb_spawn` 16 times instead of the documented maximum of 15 child spawns (root + 15 = 16 total). The 17th concurrent VM is created in violation of the `MAX_VMS_COUNT` invariant. While the over-count is bounded at +1 (not unbounded), it constitutes incorrect behavior of the CKB-VM scheduler: the enforced limit diverges from the documented and consensus-expected limit, which is the definition of the targeted scope ("Incorrect implementation or behavior of CKB-VM or system scripts").

---

### Likelihood Explanation

The attack surface is the `ckb_spawn` syscall, reachable by any V2 script deployed on-chain. No privileged access, key material, or majority hashpower is required. The script simply loops calling `ckb_spawn` until it has 16 live children; the 16th succeeds where it should fail.

---

### Recommendation

Change the comparison operator from strict-greater-than to greater-than-or-equal:

```rust
// Before (off-by-one):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
``` [5](#0-4) 

The companion C test `spawn_create_17_spawn.c` must also be updated: `SPAWN_TIMES` should be 16 and the boundary check should expect failure at `i >= 15` (i.e., the 16th spawn attempt), not `i >= 16`.

---

### Proof of Concept

1. Write a V2 lock script that calls `ckb_spawn` in a loop, recording the return code each iteration.
2. Assert that the 16th call (index 15, making total = 17) returns `SUCCESS` with the current code.
3. Assert that the 17th call (index 16) returns `CKB_MAX_VMS_SPAWNED` (error code 8).
4. With the fix applied, the 15th call (index 14, making total = 16) should be the last to succeed, and the 16th call should return `CKB_MAX_VMS_SPAWNED`.

The existing `spawn_create_17_spawn.c` already encodes exactly this scenario and passes against the buggy scheduler, confirming the off-by-one is live. [6](#0-5)

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

**File:** script/testdata/spawn_create_17_spawn.c (L90-106)
```c
        ret = ckb_spawn(0, CKB_SOURCE_CELL_DEP, 0, 0, &args);
        if (ret == CKB_SUCCESS) {
            printf("invoke spawn: %d process id: %lu\n", i, pid);
            root_process_read_fds[i] = root_read_spawn_write_pipe[0];
            root_process_write_fds[i] = spawn_read_root_write_pipe[1];
            if (i + 1 != pid) {
                printf("Unexpected process id!\n");
                return -1;
            }
            spawns = i + 1;
        } else {
            printf("invoke spawn: %d err: %d\n", i, ret);
            if ((i < 16) || (ret != CKB_MAX_VMS_SPAWNED)) {
                printf("Unexpected spawn error!\n");
                return -1;
            }
        }
```
