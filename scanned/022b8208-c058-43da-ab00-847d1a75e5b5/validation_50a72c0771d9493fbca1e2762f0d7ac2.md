Audit Report

## Title
Type ID Built-in Verifier Applied to Lock Script Groups Without Role Check, Bypassing Authorization — (`script/src/verify.rs`, `script/src/type_id.rs`)

## Summary

The `TypeIdSystemScript` built-in verifier is dispatched in `verify_script_group` based solely on `code_hash == TYPE_ID_CODE_HASH` and `hash_type == Type`, with no check on `group.group_type`. Because lock script groups are structurally built with non-empty `input_indices` and empty `output_indices`, the Type ID creation check is permanently unreachable for lock groups. Any cell whose lock script uses `TYPE_ID_CODE_HASH` with 32-byte args passes the built-in verifier unconditionally, providing zero authorization protection and allowing any party to spend the cell.

## Finding Description

**Root cause 1 — `verify_script_group` ignores `group.group_type`:**

All three dispatch paths in `verify.rs` check only `code_hash` and `hash_type`: [1](#0-0) 

The same pattern is repeated at lines 452–453 (`verify_group_with_chunk`) and 624–625 (`verify_group_with_signal`). `group.group_type` (`ScriptGroupType::Lock` vs `ScriptGroupType::Type`) is never consulted.

**Root cause 2 — Creation check in `TypeIdSystemScript::verify()` is structurally unreachable for lock groups:**

The creation/initialization check only fires when `input_indices.is_empty()`: [2](#0-1) 

For a lock group, `input_indices` is always non-empty (it contains the spending input index), so this branch is never entered.

**Root cause 3 — Lock groups structurally never have `output_indices` populated:**

In `TxData::new()`, lock groups are built exclusively from `resolved_inputs` with only `input_indices.push(i)`. Output lock scripts are never added to any lock group's `output_indices`. Only type scripts of outputs receive `output_indices.push(i)`: [3](#0-2) 

**Combined exploit flow:**

For a cell with `lock = {code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <32 bytes>}`, the lock group has `input_indices = [i]` and `output_indices = []`. `verify_script_group` dispatches to `TypeIdSystemScript::verify()`, which:

1. `args().len() == 32` → passes
2. `input_indices.len() > 1` → false → passes
3. `output_indices.len() > 1` → false → passes
4. `input_indices.is_empty()` → false → creation check **skipped**
5. Returns `Ok(TYPE_ID_CYCLES)` unconditionally [4](#0-3) 

No signature, no hash validation, no authorization of any kind is performed.

## Impact Explanation

This is an incorrect implementation of a CKB system script. The Type ID built-in verifier silently replaces the normal script execution path (`self.run(group, max_cycles)`) for any lock script matching `TYPE_ID_CODE_HASH` + `hash_type::Type`, but performs no authorization. Any cell created with this lock script configuration is freely spendable by any transaction sender, resulting in direct theft of the cell's CKB capacity. This maps to the allowed impact: **Incorrect implementation or behavior of CKB-VM or system scripts — High (10001–15000 points)**.

## Likelihood Explanation

A script author building a singleton guardian cell, protocol-controlled treasury, or upgrade-preserving cell may use `TYPE_ID_CODE_HASH` as the lock script by analogy with its type script role. The node accepts such cells at creation without error or warning. The exploit requires no special privileges — any transaction sender can submit a spending transaction with no witnesses. The condition is repeatable and deterministic once such a cell exists on-chain.

## Recommendation

In `verify_script_group`, `verify_group_with_chunk`, and `verify_group_with_signal` in `script/src/verify.rs`, add a guard on `group.group_type`:

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

Alternatively, `TypeIdSystemScript::verify()` in `script/src/type_id.rs` should assert `self.script_group.group_type == ScriptGroupType::Type` and return a validation failure otherwise.

## Proof of Concept

1. Craft a cell output: `lock = Script { code_hash: TYPE_ID_CODE_HASH, hash_type: 0x01 (Type), args: 0x00..00 (32 zero bytes) }`, `type = None`, capacity = 100 CKB.
2. Submit transaction T1 creating this cell (the output's lock is not verified at creation time — only type scripts of outputs are verified).
3. Submit transaction T2: `inputs[0]` = the cell from step 2, any output (attacker's address), no witnesses.
4. `TxData::new()` builds `lock_groups[H]` with `input_indices = [0]`, `output_indices = []`.
5. `verify_script_group` sees `code_hash == TYPE_ID_CODE_HASH` and `hash_type == Type` → dispatches to `TypeIdSystemScript::verify()`.
6. All checks pass trivially; returns `Ok(1_000_000)`.
7. T2 is accepted and committed. The attacker has stolen the cell's capacity without any key or authorization.

### Citations

**File:** script/src/verify.rs (L432-443)
```rust
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
