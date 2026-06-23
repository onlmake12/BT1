The code confirms this is a real vulnerability. Let me trace the exact path:

**Script group construction** (`script/src/types.rs` lines 716-740): Lock script groups only ever populate `input_indices`. `output_indices` is **never** populated for lock groups — only type script groups get output entries.

So for a cell with `lock = {code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <32 bytes>}` spent in a 1-input transaction:
- `input_indices = [0]`
- `output_indices = []` (always, for any lock group)

**`verify_script_group`** (`script/src/verify.rs` line 432) routes to `TypeIdSystemScript::verify()` based solely on `code_hash == TYPE_ID_CODE_HASH && hash_type == Type` — **no check on `group.group_type`**.

**`TypeIdSystemScript::verify()`** (`script/src/type_id.rs` lines 29-77):
1. `max_cycles < TYPE_ID_CYCLES` → passes
2. `args().len() != 32` → passes (attacker supplies 32-byte args)
3. `input_indices.len() > 1 || output_indices.len() > 1` → `1 > 1 || 0 > 1` → **false**, passes
4. `input_indices.is_empty()` → **false** (1 input) → creation hash check is **skipped**
5. Falls through to `Ok(TYPE_ID_CYCLES)` — **no authentication performed**

---

### Title
TYPE_ID System Script Unconditionally Succeeds as Lock Script for Single-Input Spends — (`script/src/type_id.rs`)

### Summary
`TypeIdSystemScript::verify()` does not check `group_type` and contains no authentication logic for the case where `input_indices` is non-empty and `output_indices` is empty. For a lock script group this is always the state, making TYPE_ID an always-success lock for any single-input spend.

### Finding Description
`verify_script_group` in `script/src/verify.rs` intercepts any script whose `code_hash == TYPE_ID_CODE_HASH` and `hash_type == Type` and dispatches it to `TypeIdSystemScript::verify()`, regardless of whether the script is acting as a lock or type script. [1](#0-0) 

Script group construction in `TxData::new()` only ever appends to `output_indices` for **type** script groups. Lock script groups always have `output_indices = []`. [2](#0-1) 

Inside `TypeIdSystemScript::verify()`, the only branch that performs any cryptographic check is the `input_indices.is_empty()` branch (the "creation" path). When `input_indices = [0]` and `output_indices = []` — which is the invariant state for every lock script group with one input — neither the `> 1` guard nor the `is_empty()` guard triggers, and the function returns `Ok(TYPE_ID_CYCLES)` unconditionally. [3](#0-2) 

### Impact Explanation
Any cell whose `lock` field is set to `{code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <any 32 bytes>}` can be spent by an unprivileged attacker in a transaction with exactly one input and no witness. The lock script returns success without verifying any signature or preimage, so the attacker can redirect the cell's full CKB capacity to an output they control.

### Likelihood Explanation
The attacker must first find (or create) a cell locked with TYPE_ID as a lock script. While this is an unusual configuration, it is not prevented by the protocol. A developer who misunderstands TYPE_ID's intended use (type script for uniqueness, not a lock) could deploy such a cell. Once such a cell exists on-chain, any observer can steal it with a trivial, witness-free transaction.

### Recommendation
Add a `group_type` guard at the top of `TypeIdSystemScript::verify()` (or in `verify_script_group`) that returns an error if the script is being evaluated as a lock script:

```rust
if self.script_group.group_type == ScriptGroupType::Lock {
    return Err(self.validation_failure(ERROR_ARGS)); // or a dedicated error code
}
```

Alternatively, `verify_script_group` should only short-circuit to `TypeIdSystemScript` when `group.group_type == ScriptGroupType::Type`.

### Proof of Concept
```
1. Create cell C:
     lock.code_hash = TYPE_ID_CODE_HASH
     lock.hash_type = Type
     lock.args      = [0u8; 32]   // any 32 bytes

2. Build transaction T:
     inputs  = [C]
     outputs = [attacker_cell]   // no TYPE_ID lock
     witnesses = []              // empty — no signature

3. Submit T.

4. verify_script_group is called for the lock group of C:
     group.input_indices  = [0]
     group.output_indices = []
     → TypeIdSystemScript::verify() returns Ok(TYPE_ID_CYCLES)

5. T is accepted; attacker receives C's capacity.
``` [4](#0-3) [5](#0-4)

### Citations

**File:** script/src/verify.rs (L427-444)
```rust
    fn verify_script_group(
        &self,
        group: &ScriptGroup,
        max_cycles: Cycle,
    ) -> Result<Cycle, ScriptError> {
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
        {
            let verifier = TypeIdSystemScript {
                rtx: &self.tx_data.rtx,
                script_group: group,
                max_cycles,
            };
            verifier.verify()
        } else {
            self.run(group, max_cycles)
        }
    }
```

**File:** script/src/types.rs (L716-740)
```rust
        let mut lock_groups = BTreeMap::default();
        let mut type_groups = BTreeMap::default();
        for (i, cell_meta) in resolved_inputs.iter().enumerate() {
            // here we are only pre-processing the data, verify method validates
            // each input has correct script setup.
            let output = &cell_meta.cell_output;
            let lock_group_entry = lock_groups
                .entry(output.calc_lock_hash())
                .or_insert_with(|| ScriptGroup::from_lock_script(&output.lock()));
            lock_group_entry.input_indices.push(i);
            if let Some(t) = &output.type_().to_opt() {
                let type_group_entry = type_groups
                    .entry(t.calc_script_hash())
                    .or_insert_with(|| ScriptGroup::from_type_script(t));
                type_group_entry.input_indices.push(i);
            }
        }
        for (i, output) in rtx.transaction.outputs().into_iter().enumerate() {
            if let Some(t) = &output.type_().to_opt() {
                let type_group_entry = type_groups
                    .entry(t.calc_script_hash())
                    .or_insert_with(|| ScriptGroup::from_type_script(t));
                type_group_entry.output_indices.push(i);
            }
        }
```

**File:** script/src/type_id.rs (L29-77)
```rust
    pub fn verify(&self) -> Result<Cycle, ScriptError> {
        if self.max_cycles < TYPE_ID_CYCLES {
            return Err(ScriptError::ExceededMaximumCycles(self.max_cycles));
        }
        // TYPE_ID script should only accept one argument,
        // which is the hash of all inputs when creating
        // the cell.
        if self.script_group.script.args().len() != 32 {
            return Err(self.validation_failure(ERROR_ARGS));
        }

        // There could be at most one input cell and one
        // output cell with current TYPE_ID script.
        if self.script_group.input_indices.len() > 1 || self.script_group.output_indices.len() > 1 {
            return Err(self.validation_failure(ERROR_TOO_MANY_CELLS));
        }

        // If there's only one output cell with current
        // TYPE_ID script, we are creating such a cell,
        // we also need to validate that the first argument matches
        // the hash of following items concatenated:
        // 1. First CellInput of the transaction.
        // 2. Index of the first output cell in current script group.
        if self.script_group.input_indices.is_empty() {
            let first_cell_input = self
                .rtx
                .transaction
                .inputs()
                .get(0)
                .ok_or_else(|| self.validation_failure(ERROR_ARGS))?;
            let first_output_index: u64 = self
                .script_group
                .output_indices
                .first()
                .map(|output_index| *output_index as u64)
                .ok_or_else(|| self.validation_failure(ERROR_ARGS))?;

            let mut blake2b = new_blake2b();
            blake2b.update(first_cell_input.as_slice());
            blake2b.update(&first_output_index.to_le_bytes());
            let mut ret = [0; 32];
            blake2b.finalize(&mut ret);

            if ret[..] != self.script_group.script.args().raw_data()[..] {
                return Err(self.validation_failure(ERROR_INVALID_INPUT_HASH));
            }
        }
        Ok(TYPE_ID_CYCLES)
    }
```
