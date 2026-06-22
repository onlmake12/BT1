### Title
Type ID Built-in Verifier Applied to Lock Script Groups Without Role Check, Bypassing Creation Initialization — (File: `script/src/verify.rs`, `script/src/type_id.rs`)

---

### Summary

The CKB built-in `TypeIdSystemScript` verifier is dispatched in `verify_script_group` based solely on `code_hash == TYPE_ID_CODE_HASH` and `hash_type == Type`, with no check on `group.group_type` (Lock vs Type). Because lock script groups are structurally populated differently from type script groups — lock groups never have `output_indices` populated — the Type ID creation/initialization check (`input_indices.is_empty()`) is permanently unreachable for any lock script group. A cell whose lock script uses `TYPE_ID_CODE_HASH` passes the built-in verifier unconditionally (with any 32-byte args), providing zero authorization protection. Any transaction sender can spend such a cell.

---

### Finding Description

**Root cause 1 — `verify_script_group` ignores `group.group_type`:**

In `script/src/verify.rs`, all three dispatch paths check only `code_hash` and `hash_type`:

```rust
// verify_script_group (line 432)
if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
    && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
{
    let verifier = TypeIdSystemScript { rtx: &self.tx_data.rtx, script_group: group, max_cycles };
    verifier.verify()
} else {
    self.run(group, max_cycles)
}
```

`group.group_type` (the `ScriptGroupType::Lock` / `ScriptGroupType::Type` discriminant) is never consulted. The same pattern is repeated in `verify_group_with_chunk` (line 452) and `verify_group_with_signal` (line 624). [1](#0-0) 

**Root cause 2 — `TypeIdSystemScript::verify()` creation check is structurally unreachable for lock groups:**

The creation/initialization check in `type_id.rs` only fires when `input_indices.is_empty()`:

```rust
if self.script_group.input_indices.is_empty() {
    // validate args == hash(first_input || output_index)
    ...
}
``` [2](#0-1) 

**Root cause 3 — Lock groups structurally never have `output_indices` populated, and always have `input_indices` non-empty:**

In `TxData::new()`, lock groups are built exclusively from `resolved_inputs`. Outputs' lock scripts are never added to any lock group's `output_indices`:

```rust
// inputs → lock_groups (input_indices only)
let lock_group_entry = lock_groups
    .entry(output.calc_lock_hash())
    .or_insert_with(|| ScriptGroup::from_lock_script(&output.lock()));
lock_group_entry.input_indices.push(i);

// outputs → type_groups only (output_indices)
for (i, output) in rtx.transaction.outputs().into_iter().enumerate() {
    if let Some(t) = &output.type_().to_opt() {
        type_group_entry.output_indices.push(i);
    }
}
``` [3](#0-2) 

**Combined effect:**

When a cell has `lock = {code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <any 32 bytes>}`:

| Property | Lock group | Type group |
|---|---|---|
| `input_indices` | `[i]` (spending input) | `[i]` (input cell) |
| `output_indices` | `[]` (never populated) | `[j]` (output cell, if present) |
| Creation check fires? | **Never** (`input_indices` always non-empty) | Yes (when `input_indices` is empty) |
| Args validated? | **Never** | Yes, on creation |

The Type ID verifier for a lock group reaches line 76 (`Ok(TYPE_ID_CYCLES)`) unconditionally, as long as args are exactly 32 bytes and there is at most one input with this lock. No signature, no hash validation, no authorization of any kind is performed. [4](#0-3) 

---

### Impact Explanation

A cell whose lock script is `{TYPE_ID_CODE_HASH, Type, <any 32-byte args>}` is freely spendable by any transaction sender. The built-in Type ID verifier replaces the normal script execution path (`self.run(group, max_cycles)`) but provides no authorization — it only enforces structural constraints (≤1 input, ≤1 output, and args length). For a lock group, all three structural checks pass trivially. The cell's CKB capacity can be stolen by any party who submits a valid transaction consuming it as an input.

This is a **transaction authorization** failure: the lock script, which is the sole on-chain authorization mechanism for spending a cell, is silently replaced by a no-op structural check.

---

### Likelihood Explanation

A script author who intends to create a "uniquely identified" lock (e.g., a singleton guardian cell, a protocol-controlled treasury, or a cell whose identity must be preserved across upgrades) may reach for `TYPE_ID_CODE_HASH` with `hash_type = Type` as the lock script, reasoning by analogy with how Type ID works for type scripts. The CKB documentation and tooling do not explicitly forbid this. The node accepts and commits such transactions without error. The resulting cell is silently unprotected. The likelihood is low-to-medium for protocol-level contracts and higher for less-experienced script authors.

---

### Recommendation

In `verify_script_group` (and its chunked/async variants in `script/src/verify.rs`), add a guard on `group.group_type`:

```rust
if group.group_type == ScriptGroupType::Type
    && group.script.code_hash() == TYPE_ID_CODE_HASH.into()
    && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
{
    // built-in Type ID verifier
} else {
    self.run(group, max_cycles)
}
```

Alternatively, `TypeIdSystemScript::verify()` in `script/src/type_id.rs` should assert `self.script_group.group_type == ScriptGroupType::Type` and return a validation failure otherwise. This mirrors the report's recommendation: do not allow the lock-script role to silently invoke the type-script-role verifier.

---

### Proof of Concept

1. Craft a cell output with:
   - `lock = Script { code_hash: TYPE_ID_CODE_HASH, hash_type: 0x01 (Type), args: 0x00..00 (32 zero bytes) }`
   - `type = None`
   - Any capacity (e.g., 100 CKB)

2. Submit a transaction T1 that creates this cell (the lock group for T1's inputs is unrelated; the output's lock is not verified at creation time — only type scripts of outputs are verified).

3. Submit a transaction T2 with:
   - `inputs[0]` = the cell from step 2
   - Any output (e.g., attacker's own address)
   - No witnesses

4. `TxData::new()` builds `lock_groups[H]` with `input_indices = [0]`, `output_indices = []`.

5. `verify_script_group` sees `code_hash == TYPE_ID_CODE_HASH` and `hash_type == Type` → dispatches to `TypeIdSystemScript::verify()`.

6. `TypeIdSystemScript::verify()`:
   - `args().len() == 32` ✓
   - `input_indices.len() > 1` → false ✓
   - `output_indices.len() > 1` → false ✓
   - `input_indices.is_empty()` → false → creation check **skipped**
   - Returns `Ok(1_000_000)` ✓

7. T2 is accepted and committed. The attacker has stolen the cell's capacity without any key or authorization.

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
