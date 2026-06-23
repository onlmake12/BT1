The question conflates two separate claims: a TOCTOU race and an off-by-one. Let me trace the actual code precisely.

### Title
Off-by-One in `MAX_VMS_COUNT` Guard Allows 17th VM to Boot — (`script/src/scheduler.rs`)

---

### Summary

The VM count guard in `process_message_box` uses strict `>` instead of `>=`, allowing exactly one VM beyond the intended `MAX_VMS_COUNT = 16` limit to be created by an unprivileged script author.

---

### Finding Description

The guard at line 532 of `script/src/scheduler.rs`:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
```

uses `>` (strictly greater than). When `suspended.len() + instantiated.len() == 16`, the expression `16 > 16` evaluates to `false`, so execution falls through to `boot_vm`, inserting a 17th VM into `instantiated` and `states`. [1](#0-0) 

`boot_vm` unconditionally inserts the new VM: [2](#0-1) 

The correct guard should be `>= MAX_VMS_COUNT`.

**Concrete reachable path:**

1. Root VM (1 VM) issues 15 `Message::Spawn` syscalls in a single execution slice.
2. `process_message_box` drains all 15 messages and processes them sequentially; after each `boot_vm` the counts are updated, so after all 15 the total is 16.
3. Root VM (or any child) issues one more `Message::Spawn`. The check `16 > 16` is `false` → `boot_vm` is called → 17th VM is created.
4. Only on the *next* attempt does `17 > 16` = `true` return `MAX_VMS_SPAWNED`. [3](#0-2) 

---

### TOCTOU Claim: Incorrect

The question's TOCTOU framing does not apply. `process_message_box` holds exclusive mutable access to `self` (including `suspended` and `instantiated`) for its entire duration. The scheduler is single-threaded; there is no concurrent modification window. The bug is a plain off-by-one, not a race condition. [4](#0-3) 

The referenced file `util/channel/src/lib.rs` is an unrelated re-export of `crossbeam_channel` and plays no role here. [5](#0-4) 

---

### Impact Explanation

- **Maximum VM count**: 17, not unbounded. Once 17 VMs exist, `17 > 16` = `true` blocks further spawning.
- **Memory**: Bounded to one extra VM's allocation; not unbounded.
- **Consensus**: All nodes run the same code with the same off-by-one, so they all allow 17 VMs. There is no inter-node consensus deviation.
- **Cycle accounting**: The extra VM is still subject to the global cycle limit; no cycle bypass occurs.
- **Invariant violated**: The stated invariant "total live VMs ≤ 16" is violated by exactly 1.

---

### Likelihood Explanation

Any ScriptVersion::V2 script submitted in a transaction can trigger this. No privileged access, leaked keys, or majority hashpower is required. The path is fully local-testable.

---

### Recommendation

Change the guard from `>` to `>=`:

```rust
// Before (off-by-one):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
``` [6](#0-5) 

---

### Proof of Concept

```c
// ScriptVersion::V2 script (pseudocode)
// Step 1: spawn 15 children from root → total = 16 VMs
for (int i = 0; i < 15; i++) {
    ckb_spawn(...);  // each returns SUCCESS
}
// Step 2: spawn one more — off-by-one allows it
int ret = ckb_spawn(...);
assert(ret == SUCCESS);  // passes: 16 > 16 is false, 17th VM boots

// Step 3: one more attempt is correctly blocked
ret = ckb_spawn(...);
assert(ret == MAX_VMS_SPAWNED);  // 17 > 16 is true
```

A proptest fuzzer asserting `spawned_count <= 16` would catch this immediately on the 16th successful spawn.

### Citations

**File:** script/src/scheduler.rs (L482-484)
```rust
    fn process_message_box(&mut self) -> Result<(), Error> {
        let messages: Vec<Message> = self.message_box.lock().expect("lock").drain(..).collect();
        for message in messages {
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

**File:** script/src/scheduler.rs (L1035-1036)
```rust
        self.instantiated.insert(id, (context, machine));
        self.states.insert(id, VmState::Runnable);
```

**File:** util/channel/src/lib.rs (L1-5)
```rust
//! Reexports `crossbeam_channel` to uniform the dependency version.
pub use crossbeam_channel::{
    Receiver, RecvError, RecvTimeoutError, Select, SendError, Sender, TrySendError, after, bounded,
    select, tick, unbounded,
};
```
