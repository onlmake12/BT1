Audit Report

## Title
Off-by-One in `MAX_VMS_COUNT` Guard Allows 17th VM to Boot — (`script/src/scheduler.rs`)

## Summary
The VM count guard in `process_message_box` uses strict `>` instead of `>=`, allowing exactly one VM beyond the intended `MAX_VMS_COUNT = 16` limit to be created. Any unprivileged ScriptVersion::V2 script can trigger this by spawning 16 children sequentially; the 16th spawn succeeds when it should be blocked. The stated invariant "total live VMs ≤ 16" is violated by exactly one.

## Finding Description
`MAX_VMS_COUNT` is defined as `16` at [1](#0-0)  The guard in `process_message_box` at [2](#0-1)  reads:

```rust
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {
```

When the total VM count is exactly 16, `16 > 16` evaluates to `false`, so execution falls through to `boot_vm`, which unconditionally inserts the new VM into `instantiated` and `states`: [3](#0-2) 

Only on the *next* spawn attempt does `17 > 16 = true` trigger the `MAX_VMS_SPAWNED` error. The scheduler holds exclusive mutable access to `self` throughout `process_message_box` — this is a plain off-by-one, not a TOCTOU race. [4](#0-3) 

**Exploit path:**
1. Root VM (1 VM) issues 15 `Message::Spawn` syscalls → total = 16 VMs.
2. Root VM issues one more `Message::Spawn`. Guard: `16 > 16` = `false` → `boot_vm` called → 17th VM created.
3. Only the subsequent attempt (`17 > 16` = `true`) is correctly blocked.

## Impact Explanation
This is an incorrect implementation of CKB-VM's spawn limit enforcement — the documented invariant (`MAX_VMS_COUNT = 16` as "the maximum number of VMs that can be created at the same time") is violated by exactly one. This maps to the allowed impact: **Incorrect implementation or behavior of CKB-VM or system scripts** → **High (10001–15000 points)**. The extra VM is still subject to the global cycle limit, and since all nodes run identical code, there is no consensus deviation. The impact is bounded to one extra VM allocation per script execution.

## Likelihood Explanation
Any ScriptVersion::V2 script submitted in a transaction can trigger this. No privileged access, leaked keys, or majority hashpower is required. The path is fully deterministic and locally reproducible without any special setup.

## Recommendation
Change the guard from `>` to `>=`:

```rust
// Before (off-by-one):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

This ensures the check fires when the count reaches 16, preventing the 17th VM from being created. [5](#0-4) 

## Proof of Concept
A unit test or proptest fuzzer asserting `spawned_count <= 16` after each successful spawn will fail on the 16th successful spawn. Concretely:

```rust
// In a ScriptVersion::V2 script:
// Spawn 15 children from root → total = 16
for i in 0..15 {
    assert_eq!(ckb_spawn(...), SUCCESS);
}
// 16th spawn: guard 16 > 16 = false → succeeds (should be blocked)
assert_eq!(ckb_spawn(...), SUCCESS);       // off-by-one: passes
// 17th spawn: guard 17 > 16 = true → correctly blocked
assert_eq!(ckb_spawn(...), MAX_VMS_SPAWNED);
```

An invariant test checking `scheduler.suspended.len() + scheduler.instantiated.len() <= MAX_VMS_COUNT` after every `process_message_box` call would catch this immediately.

### Citations

**File:** script/src/scheduler.rs (L34-34)
```rust
pub const MAX_VMS_COUNT: u64 = 16;
```

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
