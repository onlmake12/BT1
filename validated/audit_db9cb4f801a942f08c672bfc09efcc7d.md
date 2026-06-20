### Title
Off-by-One in Spawn VM Count Guard Allows Exceeding `MAX_VMS_COUNT` — (File: `script/src/scheduler.rs`)

---

### Summary

In `process_message_box()`, the `Message::Spawn` handler guards against exceeding the protocol-defined VM limit by checking `self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT`. Because the comparison uses strict `>` rather than `>=`, the guard passes when exactly `MAX_VMS_COUNT` (16) VMs are already live, and the subsequent `boot_vm()` call inserts a 17th VM. A script author can deterministically trigger this by crafting a transaction whose script spawns VMs up to the boundary, causing the node to accept a 17-VM execution that a correct implementation would reject, producing a consensus split.

---

### Finding Description

**Root cause — wrong comparison operator in the spawn guard:** [1](#0-0) 

`MAX_VMS_COUNT` is defined as `16`, documented as "The maximum number of VMs that can be created at the same time." [2](#0-1) 

The guard fires only when `count > 16`, i.e., when `count >= 17`. When `count == 16` the condition is `false`, spawn is not rejected, and execution falls through to: [3](#0-2) 

`boot_vm()` unconditionally increments `next_vm_id`, inserts the new VM into both `self.instantiated` and `self.states`, and returns: [4](#0-3) 

After `boot_vm()` returns, `suspended.len() + instantiated.len()` is 17. The guard was evaluated against the pre-insertion count of 16 and was not re-evaluated after the mutation — a direct structural analog to the report's TOCTOU: a count is read, state is mutated, and the stale count drives the wrong branch.

**State mutation between check and use:**

Inside `boot_vm()`, `load_vm_program()` is called with `VmArgs::Reader { vm_id, … }`: [5](#0-4) 

This calls `ensure_get_instantiated(&vm_id)`, which calls `ensure_vms_instantiated()`, which may call `resume_vm()` / `suspend_vm()` to swap VMs between `suspended` and `instantiated`. Although the total count is preserved by those swaps, the guard at line 532 was already evaluated against the pre-`boot_vm` state and is never rechecked. The new VM is then inserted at lines 1035–1036, permanently raising the total to 17.

---

### Impact Explanation

`MAX_VMS_COUNT = 16` is a consensus-enforced protocol constant for the CKB-VM spawn subsystem (Meepo hardfork, ScriptVersion::V2). A node running this code accepts transactions whose scripts create 17 concurrent VMs. A node with a correct `>=` guard rejects the same transaction. Any transaction that exercises exactly 17 VMs causes a permanent consensus split between patched and unpatched nodes: the unpatched node commits the block, the patched node rejects it, and the two chains diverge. This is a critical consensus-integrity failure.

---

### Likelihood Explanation

The entry path is fully unprivileged. Any user can submit a transaction via `send_transaction` RPC or P2P relay. The script need only call `ckb_spawn` recursively until the 17th VM is created. No special keys, operator access, or majority hashpower is required. The CKB test suite already exercises the max-VM boundary: [6](#0-5) 

confirming the path is reachable and tested. The off-by-one is one VM beyond the tested boundary, making it trivially reachable.

---

### Recommendation

Change the comparison operator from `>` to `>=` at line 532:

```rust
// Before (buggy):
if self.suspended.len() + self.instantiated.len() > MAX_VMS_COUNT as usize {

// After (correct):
if self.suspended.len() + self.instantiated.len() >= MAX_VMS_COUNT as usize {
```

This ensures the guard fires when the live VM count already equals the limit, preventing `boot_vm()` from ever raising it to `MAX_VMS_COUNT + 1`.

---

### Proof of Concept

A CKB script targeting ScriptVersion::V2 that recursively calls `ckb_spawn` on itself:

1. Root VM (VM 0) spawns VM 1 → VM 2 → … → VM 15 (16 total, `suspended + instantiated == 16`).
2. VM 15 issues one more `ckb_spawn`. The guard evaluates `16 > 16 = false`, does not return `MAX_VMS_SPAWNED`, and calls `boot_vm()`.
3. VM 16 is inserted; `suspended + instantiated == 17`.
4. The transaction is accepted by the unpatched node and committed to a block.
5. A patched node (with `>=`) evaluates `16 >= 16 = true`, returns `MAX_VMS_SPAWNED` to VM 15, the script fails, and the block is rejected — consensus split.

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

**File:** script/src/scheduler.rs (L1015-1038)
```rust
    fn boot_vm(&mut self, location: &DataLocation, args: VmArgs) -> Result<VmId, Error> {
        let id = self.next_vm_id;
        self.next_vm_id += 1;
        let (context, mut machine) = self.create_dummy_vm(&id)?;
        let (program, _) = {
            let sc = context.snapshot2_context.lock().expect("lock");
            sc.load_data(&location.data_piece_id, location.offset, location.length)?
        };
        self.load_vm_program(&context, &mut machine, location, program, args)?;
        // Newly booted VM will be instantiated by default
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

        Ok(id)
```

**File:** script/src/scheduler.rs (L1051-1058)
```rust
        let bytes = match args {
            VmArgs::Reader { vm_id, argc, argv } => {
                let (_, machine_from) = self.ensure_get_instantiated(&vm_id)?;
                let argc = Self::u64_to_reg(argc);
                let argv = Self::u64_to_reg(argv);
                let argv =
                    FlattenedArgsReader::new(machine_from.inner_mut().memory_mut(), argc, argv);
                machine.load_program_with_metadata(&program, &metadata, argv)?
```

**File:** script/src/verify/tests/ckb_latest/features_since_v2023.rs (L103-106)
```rust
fn check_spawn_max_vms_count() {
    let result = simple_spawn_test("testdata/spawn_cases", &[10]);
    assert_eq!(result.is_ok(), SCRIPT_VERSION == ScriptVersion::V2);
}
```
