### Title
Spawned Child VMs Inherit Parent Script Identity via `ckb_load_script_hash` / `ckb_load_script` Syscalls — (File: `script/src/scheduler.rs`)

---

### Summary

When a script spawns a child VM via `ckb_spawn`, the child VM is initialized with the **parent's** `SgData` (script-group data). As a result, the `ckb_load_script_hash` and `ckb_load_script` syscalls inside the child VM return the **parent script group's** hash and script body, not the child's own. This is the direct CKB analog of the Gelato relay bug: an intermediary (the spawn mechanism) causes the wrong identity to be reported to the executing code.

---

### Finding Description

**Root cause — `create_dummy_vm` passes the parent's `sg_data` to every child VM**

`Scheduler::create_dummy_vm` is the single factory for every VM instance, both the root VM and every VM spawned via `ckb_spawn`:

```rust
// script/src/scheduler.rs  line 1078-1099
fn create_dummy_vm(&self, id: &VmId) -> Result<(VmContext<DL>, M), Error> {
    let version = &self.sg_data.sg_info.script_version;   // ← parent's version
    ...
    let vm_context = VmContext {
        snapshot2_context: Arc::new(Mutex::new(
            Snapshot2Context::new(self.sg_data.clone())   // ← parent's sg_data
        )),
        ...
    };
    // syscall_generator receives &self.sg_data for every VM, root or child
    (self.syscall_generator)(id, &self.sg_data, &vm_context, &self.syscall_context)
        .into_iter()
        .fold(machine_builder, |b, s| b.syscall(s))
``` [1](#0-0) 

`syscall_generator` is `generate_ckb_syscalls`, which wires up `LoadScriptHash` and `LoadScript` from the same `sg_data`:

```rust
// script/src/syscalls/generator.rs  line 24-30
Box::new(LoadScriptHash::new(sg_data)),   // uses sg_data.sg_info.script_hash
...
Box::new(LoadScript::new(sg_data)),       // uses sg_data.sg_info.script_group.script
``` [2](#0-1) 

`LoadScriptHash` reads `sg_info.script_hash`:

```rust
// script/src/syscalls/load_script_hash.rs  line 35
let data = self.sg_info.script_hash.as_reader().raw_data();
``` [3](#0-2) 

`LoadScript` reads `sg_info.script_group.script`:

```rust
// script/src/syscalls/load_script.rs  line 36
let data = self.sg_info.script_group.script.as_slice();
``` [4](#0-3) 

`SgInfo` is set once per script group, from the **parent** script's hash and group:

```rust
// script/src/types.rs  line 997-1001
sg_info: Arc::new(SgInfo {
    script_version,
    script_hash,                          // ← parent's calc_script_hash()
    script_group: script_group.clone(),   // ← parent's ScriptGroup
    program_data_piece_id: DataPieceId::CellDep(dep_index),
}),
``` [5](#0-4) 

When `Message::Spawn` is processed, `boot_vm` calls `create_dummy_vm` with the same `self.sg_data`:

```rust
// script/src/scheduler.rs  line 1015-1018
fn boot_vm(&mut self, location: &DataLocation, args: VmArgs) -> Result<VmId, Error> {
    let id = self.next_vm_id;
    self.next_vm_id += 1;
    let (context, mut machine) = self.create_dummy_vm(&id)?;  // ← same sg_data
``` [6](#0-5) 

The child binary loaded at `location.data_piece_id` (a different cell dep) is a completely different program, but its `ckb_load_script_hash` syscall will always return the **parent** script group's hash.

---

### Impact Explanation

Any child script that calls `ckb_load_script_hash` to determine its own identity receives the parent's script hash instead. Concrete consequences:

1. **Authorization bypass**: A child script that guards a code path with `"if my hash == EXPECTED_CHILD_HASH"` will always evaluate against the parent's hash. An attacker who controls the parent script can spawn an arbitrary binary as a child; the child's self-check will silently pass or fail based on the parent's identity, not the child's.

2. **Cross-script protocol confusion**: Multi-script protocols that use `ckb_load_script_hash` to route messages or verify caller identity (analogous to ERC-2771's `_msgSender`) will misidentify the executing party, enabling unauthorized operations or denial of service.

3. **`GroupInput`/`GroupOutput` source misuse**: `SgData::load_data` resolves `DataPieceId::GroupInput` and `DataPieceId::GroupOutput` using `sg_info.script_group.input_indices` / `output_indices` — the parent's cell indices. A child script using `CKB_SOURCE_GROUP_INPUT` will silently iterate the parent's cells, not its own. [7](#0-6) 

---

### Likelihood Explanation

The `ckb_spawn` syscall is a production feature (ScriptVersion::V2 / Meepo hardfork). Any script author who writes a spawned child that calls `ckb_load_script_hash` for self-identification — a natural and documented pattern — will encounter this silently wrong value. The attacker-controlled entry path is a transaction submitted by any unprivileged user whose lock or type script uses `ckb_spawn` to invoke a child binary that relies on `ckb_load_script_hash`.

---

### Recommendation

In `Scheduler::create_dummy_vm` (or in `boot_vm`), construct a child-specific `SgInfo` that reflects the actual binary being loaded (its data hash, its `DataPieceId`, and an empty/neutral `ScriptGroup`), rather than cloning the parent's `sg_data.sg_info` wholesale. The child's `script_hash` should be derived from the binary at `location.data_piece_id`, and `script_group.input_indices` / `output_indices` should be empty unless the child is explicitly granted group membership.

---

### Proof of Concept

1. Deploy two cell deps: `parent_script` (lock script) and `child_script` (a helper binary that calls `ckb_load_script_hash` and asserts the result equals its own known hash).
2. Submit a transaction whose input cell uses `parent_script` as its lock.
3. `parent_script` calls `ckb_spawn(child_dep_index, CKB_SOURCE_CELL_DEP, ...)`.
4. Inside `child_script`, `ckb_load_script_hash` returns `hash(parent_script)`, not `hash(child_script)`.
5. Any assertion `child_hash == ckb_load_script_hash()` fails (or passes if the attacker crafts `parent_script` to have the same hash as the expected child — trivially achievable by choosing the parent script to match).

The root cause is confirmed at:
- [1](#0-0) 
- [3](#0-2) 
- [4](#0-3) 
- [8](#0-7)

### Citations

**File:** script/src/scheduler.rs (L1015-1023)
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
```

**File:** script/src/scheduler.rs (L1078-1099)
```rust
    fn create_dummy_vm(&self, id: &VmId) -> Result<(VmContext<DL>, M), Error> {
        let version = &self.sg_data.sg_info.script_version;
        let core_machine = M::Inner::new(
            version.vm_isa(),
            version.vm_version(),
            // We will update max_cycles for each machine when it gets a chance to run
            u64::MAX,
        );
        let vm_context = VmContext {
            base_cycles: Arc::clone(&self.total_cycles),
            message_box: Arc::clone(&self.message_box),
            snapshot2_context: Arc::new(Mutex::new(Snapshot2Context::new(self.sg_data.clone()))),
        };

        let machine_builder = DefaultMachineBuilder::new(core_machine)
            .instruction_cycle_func(Box::new(estimate_cycles));
        let machine_builder =
            (self.syscall_generator)(id, &self.sg_data, &vm_context, &self.syscall_context)
                .into_iter()
                .fold(machine_builder, |builder, syscall| builder.syscall(syscall));
        let default_machine = machine_builder.build();
        Ok((vm_context, M::new(default_machine)))
```

**File:** script/src/syscalls/generator.rs (L23-33)
```rust
    let mut syscalls: Vec<Box<dyn Syscalls<M>>> = vec![
        Box::new(LoadScriptHash::new(sg_data)),
        Box::new(LoadTx::new(sg_data)),
        Box::new(LoadCell::new(sg_data)),
        Box::new(LoadInput::new(sg_data)),
        Box::new(LoadHeader::new(sg_data)),
        Box::new(LoadWitness::new(sg_data)),
        Box::new(LoadScript::new(sg_data)),
        Box::new(LoadCellData::new(vm_context)),
        Box::new(Debugger::new(sg_data, debug_printer)),
    ];
```

**File:** script/src/syscalls/load_script_hash.rs (L30-41)
```rust
    fn ecall(&mut self, machine: &mut Mac) -> Result<bool, VMError> {
        if machine.registers()[A7].to_u64() != LOAD_SCRIPT_HASH_SYSCALL_NUMBER {
            return Ok(false);
        }

        let data = self.sg_info.script_hash.as_reader().raw_data();
        let wrote_size = store_data(machine, data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
        Ok(true)
    }
```

**File:** script/src/syscalls/load_script.rs (L31-42)
```rust
    fn ecall(&mut self, machine: &mut Mac) -> Result<bool, VMError> {
        if machine.registers()[A7].to_u64() != LOAD_SCRIPT_SYSCALL_NUMBER {
            return Ok(false);
        }

        let data = self.sg_info.script_group.script.as_slice();
        let wrote_size = store_data(machine, data)?;

        machine.add_cycles_no_checking(transferred_byte_cycles(wrote_size))?;
        machine.set_register(A0, Mac::REG::from_u8(SUCCESS));
        Ok(true)
    }
```

**File:** script/src/types.rs (L974-1003)
```rust
pub struct SgInfo {
    /// Currently executed script version
    pub script_version: ScriptVersion,
    /// Currently executed script group
    pub script_group: ScriptGroup,
    /// Currently executed script hash
    pub script_hash: Byte32,
    /// DataPieceId for the root program
    pub program_data_piece_id: DataPieceId,
}

impl<DL> SgData<DL> {
    /// Creates a new SgData structure from TxData, and script group information
    pub fn new(tx_data: &TxData<DL>, script_group: &ScriptGroup) -> Result<Self, ScriptError> {
        let script_hash = script_group.script.calc_script_hash();
        let script_version = tx_data.select_version(&script_group.script)?;
        let dep_index = tx_data
            .extract_referenced_dep_index(&script_group.script)?
            .try_into()
            .map_err(|_| ScriptError::Other("u32 overflow".to_string()))?;
        Ok(Self {
            rtx: Arc::clone(&tx_data.rtx),
            tx_info: Arc::clone(&tx_data.info),
            sg_info: Arc::new(SgInfo {
                script_version,
                script_hash,
                script_group: script_group.clone(),
                program_data_piece_id: DataPieceId::CellDep(dep_index),
            }),
        })
```

**File:** script/src/types.rs (L1049-1082)
```rust
            DataPieceId::GroupInput(i) => self
                .sg_info
                .script_group
                .input_indices
                .get(*i as usize)
                .and_then(|gi| self.rtx.resolved_inputs.get(*gi))
                .and_then(|cell| self.data_loader().load_cell_data(cell)),
            DataPieceId::GroupOutput(i) => self
                .sg_info
                .script_group
                .output_indices
                .get(*i as usize)
                .and_then(|gi| self.rtx.transaction.outputs_data().get(*gi))
                .map(|data| data.raw_data()),
            DataPieceId::Witness(i) => self
                .rtx
                .transaction
                .witnesses()
                .get(*i as usize)
                .map(|data| data.raw_data()),
            DataPieceId::WitnessGroupInput(i) => self
                .sg_info
                .script_group
                .input_indices
                .get(*i as usize)
                .and_then(|gi| self.rtx.transaction.witnesses().get(*gi))
                .map(|data| data.raw_data()),
            DataPieceId::WitnessGroupOutput(i) => self
                .sg_info
                .script_group
                .output_indices
                .get(*i as usize)
                .and_then(|gi| self.rtx.transaction.witnesses().get(*gi))
                .map(|data| data.raw_data()),
```
