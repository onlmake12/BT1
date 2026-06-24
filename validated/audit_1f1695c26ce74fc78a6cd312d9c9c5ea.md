All three code paths are confirmed against the actual source.

**Code path 1 — `verify_script_group` has no `group_type` guard:** [1](#0-0) 

The dispatch to `TypeIdSystemScript::verify()` fires on `code_hash` + `hash_type` alone, for both lock and type groups.

**Code path 2 — lock groups never receive `output_indices`:** [2](#0-1) [3](#0-2) 

Only type groups get `output_indices` pushed. Lock groups always have `output_indices = []`.

**Code path 3 — `TypeIdSystemScript::verify()` falls through to `Ok(TYPE_ID_CYCLES)` with no auth:** [4](#0-3) 

With `input_indices = [0]` and `output_indices = []`: guard at line 42 evaluates `1 > 1 || 0 > 1 = false`; guard at line 52 evaluates `false` (non-empty input list), skipping the creation-path hash check entirely. The function returns `Ok(TYPE_ID_CYCLES)` unconditionally.

All three claims are exactly confirmed by the code. The vulnerability is valid.

---

Audit Report

## Title
TYPE_ID System Script Unconditionally Succeeds as Lock Script for Single-Input Spends — (`script/src/type_id.rs`)

## Summary
`TypeIdSystemScript::verify()` contains no `group_type` guard. When invoked for a lock script group (which always has `output_indices = []` and `input_indices = [i]`), all three guards in `verify()` pass without performing any cryptographic check, and the function returns `Ok(TYPE_ID_CYCLES)`. Any cell whose `lock` field references `TYPE_ID_CODE_HASH` with `hash_type: Type` and 32-byte args can be unconditionally spent by an unprivileged attacker in a witness-free transaction.

## Finding Description
`verify_script_group` in `script/src/verify.rs` (lines 432–440) intercepts any script group whose `code_hash == TYPE_ID_CODE_HASH` and `hash_type == ScriptHashType::Type` and dispatches it to `TypeIdSystemScript::verify()`, with no check on `group.group_type`. This means lock script groups are handled identically to type script groups.

Script group construction in `TxData::new()` (`script/src/types.rs`, lines 722–725 and 733–739) only ever appends to `output_indices` for type script groups. Lock script groups always have `output_indices = []`.

Inside `TypeIdSystemScript::verify()` (`script/src/type_id.rs`, lines 29–76), the guards are:
1. `max_cycles < TYPE_ID_CYCLES` — passes with sufficient cycles.
2. `args().len() != 32` — passes when attacker supplies 32-byte args.
3. `input_indices.len() > 1 || output_indices.len() > 1` — evaluates to `1 > 1 || 0 > 1 = false`, passes.
4. `input_indices.is_empty()` — `false` (one input present), so the creation-path hash check is skipped entirely.

The function falls through to `Ok(TYPE_ID_CYCLES)` with no cryptographic check performed.

## Impact Explanation
This is an incorrect implementation of a CKB system script, matching the allowed impact: **"Incorrect implementation or behavior of CKB-VM or system scripts" (High, 10001–15000 points)**. Any cell locked with TYPE_ID as a lock script can be unconditionally stolen by any observer. The attacker receives the cell's full CKB capacity with a trivial, witness-free transaction.

## Likelihood Explanation
The attacker must find or create a cell with `lock.code_hash = TYPE_ID_CODE_HASH`, `lock.hash_type = Type`, and `lock.args` of exactly 32 bytes. This is an unusual but valid on-chain configuration reachable by any developer who misunderstands TYPE_ID's intended role. Once such a cell exists, any network observer can exploit it immediately and repeatedly with no special privileges, no witness, and no prior knowledge beyond the cell's existence.

## Recommendation
Add a `group_type` guard at the entry point of `TypeIdSystemScript::verify()` in `script/src/type_id.rs`:

```rust
use crate::ScriptGroupType;

pub fn verify(&self) -> Result<Cycle, ScriptError> {
    if self.script_group.group_type != ScriptGroupType::Type {
        return Err(self.validation_failure(ERROR_ARGS));
    }
    // ... existing checks
}
```

Alternatively, apply the guard in `verify_script_group` and `verify_group_with_chunk` in `script/src/verify.rs` so that the TYPE_ID fast-path is only taken when `group.group_type == ScriptGroupType::Type`.

## Proof of Concept
```
1. Create cell C on-chain:
     lock.code_hash = TYPE_ID_CODE_HASH
     lock.hash_type = Type
     lock.args      = [0u8; 32]   // any 32 bytes

2. Build transaction T:
     inputs    = [OutPoint(C)]
     outputs   = [attacker_cell with arbitrary lock]
     witnesses = []               // empty — no signature required

3. Submit T to the network.

4. verify_script_group is called for the lock group of C:
     group.group_type     = Lock
     group.input_indices  = [0]
     group.output_indices = []
     → TypeIdSystemScript::verify() returns Ok(TYPE_ID_CYCLES)

5. T is accepted; attacker receives C's full CKB capacity.
```

### Citations

**File:** script/src/verify.rs (L432-440)
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
```

**File:** script/src/types.rs (L722-725)
```rust
            let lock_group_entry = lock_groups
                .entry(output.calc_lock_hash())
                .or_insert_with(|| ScriptGroup::from_lock_script(&output.lock()));
            lock_group_entry.input_indices.push(i);
```

**File:** script/src/types.rs (L733-739)
```rust
        for (i, output) in rtx.transaction.outputs().into_iter().enumerate() {
            if let Some(t) = &output.type_().to_opt() {
                let type_group_entry = type_groups
                    .entry(t.calc_script_hash())
                    .or_insert_with(|| ScriptGroup::from_type_script(t));
                type_group_entry.output_indices.push(i);
            }
```

**File:** script/src/type_id.rs (L42-76)
```rust
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
```
