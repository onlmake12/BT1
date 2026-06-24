Audit Report

## Title
Unprivileged Child VM Can Steal Sibling Exit Codes via Unchecked `wait` Syscall — (`script/src/scheduler.rs`)

## Summary
The CKB-VM `Scheduler` stores terminated VM exit codes in a shared `terminated_vms: BTreeMap<VmId, i8>` map. The `wait` syscall accepts an arbitrary `target_id` from register A0 with no parent-child ownership check, allowing any VM within the same scheduler to consume any other VM's exit code. Combined with the deterministic highest-ID-first scheduling policy, a malicious child VM can reliably steal a sibling's exit code before the legitimate parent reads it, causing the parent to receive `WAIT_FAILURE` instead of the verifier's result.

## Finding Description

**Root cause — no ownership check in `wait` syscall:**

In `script/src/syscalls/wait.rs` lines 37–49, `target_id` is taken directly from register A0 with no validation:

```rust
let target_id = machine.registers()[A0].to_u64();
```

The `Message::Wait` is pushed to the shared message box with the caller's `vm_id` and the attacker-controlled `target_id`.

**Shared, unguarded `terminated_vms` map:**

`script/src/scheduler.rs` line 113 declares `terminated_vms: BTreeMap<VmId, i8>` as a flat map with no ownership metadata. When `process_message_box` handles a `Wait` message (lines 564–592), it checks only whether `args.target_id` exists in `terminated_vms`, delivers the exit code to the calling VM, and **removes the entry** via `retain`:

```rust
self.terminated_vms.retain(|id, _| id != &args.target_id);
```

There is no check that the calling VM is the spawner of `args.target_id`. Any VM can consume any entry.

**Deterministic scheduling advantage for attacker:**

`iterate_prepare_machine` (lines 336–348) selects the runnable VM with the **largest ID** via `.iter().rev()`. VM IDs are assigned sequentially from `FIRST_VM_ID` (line 1016–1017 in `boot_vm`). A malicious child spawned after the legitimate verifier always has a higher ID and is therefore always scheduled before the root VM (ID 0) when both are runnable.

**Attack trace:**
1. Root VM (ID 0) spawns verifier (ID 1) and malicious child (ID 2).
2. Verifier (ID 1) terminates; `iterate_process_results` inserts `terminated_vms[1] = 0` and removes VM 1 from `states` (lines 364, 402–404). No waiting VM is in `VmState::Wait` for ID 1 yet, so no direct wakeup occurs.
3. Next iteration: malicious child (ID 2, highest runnable) executes and calls `wait(1)`.
4. `process_message_box` finds `terminated_vms[1]`, delivers exit code to VM 2, removes the entry (lines 565–575).
5. Root VM (ID 0) calls `wait(1)`: `terminated_vms` no longer contains `1`, `states` no longer contains `1` → `WAIT_FAILURE` (code 5) is returned (lines 578–583).

**Existing checks are insufficient:**

The `Message::Spawn` handler validates fd ownership (`self.fds.get(fd) != Some(&vm_id)`) and the `Message::Close`/`FdRead`/`FdWrite` handlers validate fd ownership. No analogous ownership check exists for `Message::Wait`. The `Scheduler` struct has no `spawner` or `parent_vm` map anywhere.

**Secondary impact — induced deadlock:**

A malicious child can call `wait(0)` while the root VM is simultaneously waiting for the malicious child. Both VMs enter `VmState::Wait`, no runnable VM exists, and `iterate_prepare_machine` returns `Error::Unexpected("A deadlock situation has been reached!")` (line 344–346), causing unconditional script failure.

## Impact Explanation

This is **Incorrect implementation or behavior of CKB-VM** — High severity (10001–15000 points).

Concrete impacts:
- **Authorization bypass / exit-code corruption:** Lock scripts that spawn a trusted verifier child and gate transaction validity on its exit code (a standard CKB spawn-based composition pattern) can have that exit code stolen by a malicious sibling. The parent receives `WAIT_FAILURE` (5) instead of the verifier's result. If the parent conflates `WAIT_FAILURE` with a non-zero verifier exit code, authorization is bypassed.
- **Induced deadlock / unconditional script failure:** A malicious child calling `wait(0)` while the root VM waits for it creates a circular wait, causing the scheduler to abort with a deadlock error, unconditionally failing the script.

Both impacts are directly caused by the CKB-VM scheduler's missing ownership enforcement, not by any external dependency.

## Likelihood Explanation

- VM IDs are sequential and predictable; a child can call `ckb_process_id()` (syscall 2603) to learn its own ID and infer sibling IDs.
- The highest-ID-first scheduling policy is deterministic, not probabilistic — a later-spawned malicious child is **always** scheduled before the root VM after a sibling terminates.
- The attack is reachable by any transaction submitter who can influence which cell dep is loaded as a child script. Extensible lock scripts that accept user-specified plugin cell deps (a realistic and documented CKB pattern) are directly vulnerable.
- No privileged access, key material, or network majority is required.

## Recommendation

Track the spawner of each VM and enforce that a VM may only `wait` on VMs it directly spawned:

```rust
// Add to Scheduler struct:
spawner: BTreeMap<VmId, VmId>,  // child_vm_id -> parent_vm_id

// In Message::Spawn handler, after boot_vm succeeds:
self.spawner.insert(spawned_vm_id, vm_id);

// In Message::Wait handler, before processing:
if self.spawner.get(&args.target_id) != Some(&vm_id) {
    let (_, machine) = self.ensure_get_instantiated(&vm_id)?;
    machine.inner_mut().set_register(A0, Self::u8_to_reg(WAIT_FAILURE));
    continue;
}
```

Also clean up `spawner` entries when a VM terminates (in `iterate_process_results`) to avoid unbounded growth.

## Proof of Concept

**Minimal manual test:**

1. Deploy a verifier cell (exits with code 0).
2. Deploy a malicious plugin cell that calls `ckb_wait(1, &code)` in a loop until it returns `CKB_SUCCESS`, then exits.
3. Deploy a root lock script that spawns verifier (→ ID 1), spawns plugin (→ ID 2), then calls `ckb_wait(1, &code)` and returns `code`.
4. Submit a transaction using this lock script with the plugin cell dep at the user-controlled index.
5. Observe: the malicious plugin (ID 2, scheduled first) consumes `terminated_vms[1]`; the root VM's `ckb_wait(1, &code)` returns `WAIT_FAILURE` (5); the script fails or misroutes control flow.

**Invariant to fuzz:** For any scheduler execution where VM A spawns VM B and VM C (C is malicious), assert that after VM B terminates, VM A's `wait(B)` always returns `SUCCESS` with B's exit code — never `WAIT_FAILURE`. The current implementation violates this invariant whenever C calls `wait(B)` before A does.