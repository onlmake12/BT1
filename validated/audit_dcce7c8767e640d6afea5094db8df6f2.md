The code is now fully traced. Let me confirm the exact dispatch path and the missing guard.

### Title
TYPE_ID Built-in Verifier Dispatched for Lock Script Groups Without `group_type` Guard, Allowing Unconditional Spend of Any Cell Locked by TYPE_ID — (`script/src/verify.rs`, `script/src/type_id.rs`)

---

### Summary

`verify_script_group` intercepts any `ScriptGroup` whose `code_hash == TYPE_ID_CODE_HASH && hash_type == Type` and routes it to `TypeIdSystemScript::verify`, regardless of whether the group is a **lock** group or a **type** group. `TypeIdSystemScript::verify` only performs a creation-hash check when `input_indices.is_empty()` (creation mode). When spending an existing cell (`input_indices=[i]`, `output_indices=[]`), it skips all checks and returns `Ok(TYPE_ID_CYCLES)` unconditionally. Any cell whose lock script is the TYPE_ID built-in (32-byte args) is therefore freely spendable by anyone with no witness or signature.

---

### Finding Description

**Step 1 — Group construction (`TxData::new`)**

`TxData::new` unconditionally inserts every input cell's lock script into `lock_groups`, keyed by `calc_lock_hash()`: [1](#0-0) 

If the input cell's lock is `{code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <32 bytes>}`, the resulting `ScriptGroup` has `group_type = ScriptGroupType::Lock`, `input_indices = [i]`, `output_indices = []`.

**Step 2 — Dispatch in `verify_script_group`**

The dispatch condition checks only the script's `code_hash` and `hash_type`; it never inspects `group.group_type`: [2](#0-1) 

A lock group whose script happens to be the TYPE_ID built-in is therefore routed to `TypeIdSystemScript::verify` instead of the normal VM runner.

**Step 3 — `TypeIdSystemScript::verify` has no authorization logic**

The verifier performs three checks:

1. `max_cycles >= TYPE_ID_CYCLES` — passes trivially.
2. `args().len() == 32` — passes (the cell was created with 32-byte args).
3. `input_indices.len() <= 1 && output_indices.len() <= 1` — passes (1 input, 0 outputs).

Then the only substantive check — the creation-hash validation — is guarded by `input_indices.is_empty()`: [3](#0-2) 

Because `input_indices = [i]` (not empty), the branch is skipped entirely and the function returns `Ok(TYPE_ID_CYCLES)`. No witness, no signature, no proof of ownership is ever required.

**Step 4 — `groups()` chains lock groups first** [4](#0-3) 

Lock groups are iterated before type groups, so the malicious lock group is processed and passes before any type-script checks run.

---

### Impact Explanation

Any live cell whose `lock` field is `{code_hash: TYPE_ID_CODE_HASH, hash_type: Type, args: <any 32 bytes>}` can be spent by an unprivileged attacker in a transaction with no witness. The attacker redirects the cell's full CKB capacity to an output they control. This is a complete bypass of lock-script authorization for the affected cells — consensus-level economy damage.

---

### Likelihood Explanation

Using TYPE_ID as a lock script is non-standard but not prevented by any consensus rule. Developers experimenting with TYPE_ID, tooling that auto-assigns TYPE_ID to both lock and type fields, or contracts that programmatically construct scripts could produce such cells. Once such a cell exists on-chain, the exploit requires only submitting a standard transaction — no special privileges, no hashpower, no social engineering.

---

### Recommendation

Add a `group_type` guard in `verify_script_group` (and `verify_group_with_chunk` / `verify_group_with_signal`) so that the TYPE_ID built-in verifier is only invoked for **type** script groups:

```rust
// verify.rs – verify_script_group
if group.group_type == ScriptGroupType::Type          // ← add this guard
    && group.script.code_hash() == TYPE_ID_CODE_HASH.into()
    && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
{
    ...TypeIdSystemScript::verify()...
} else {
    self.run(group, max_cycles)
}
```

The same guard must be applied consistently in `verify_group_with_chunk` [5](#0-4)  and `verify_group_with_signal` [6](#0-5) .

---

### Proof of Concept

```rust
// Construct a cell whose lock IS the TYPE_ID built-in (32-byte args).
let type_id_lock = Script::new_builder()
    .code_hash(TYPE_ID_CODE_HASH)
    .hash_type(ScriptHashType::Type.into())
    .args(Bytes::from(vec![0u8; 32]))   // any 32 bytes
    .build();

let input_cell = CellOutputBuilder::default()
    .capacity(capacity_bytes!(1000))
    .lock(type_id_lock.clone())         // TYPE_ID as LOCK
    .build();

let output_cell = CellOutputBuilder::default()
    .capacity(capacity_bytes!(999))
    .lock(attacker_lock_script())       // attacker's own lock
    .build();

let input = CellInput::new(OutPoint::new(h256!("0xdead").into(), 0), 0);

let transaction = TransactionBuilder::default()
    .input(input.clone())
    .output(output_cell)
    // NO witness — no signature required
    .build();

let resolved_input = CellMetaBuilder::from_cell_output(input_cell, Bytes::new())
    .out_point(input.previous_output())
    .build();

let rtx = ResolvedTransaction {
    transaction,
    resolved_cell_deps: vec![],
    resolved_inputs: vec![resolved_input],
    resolved_dep_groups: vec![],
};

// Expected (buggy): Ok(TYPE_ID_CYCLES) — lock bypassed, theft succeeds.
// Expected (fixed):  Err(ScriptError::...) — lock must reject unsigned spend.
let result = verifier.verify_without_limit(script_version, &rtx);
assert!(result.is_ok()); // demonstrates the bypass
```

The lock group for `type_id_lock` lands in `lock_groups` with `input_indices=[0]`, `output_indices=[]`. `verify_script_group` routes it to `TypeIdSystemScript::verify`. `input_indices.is_empty()` is `false`, so the creation check is skipped, and `Ok(TYPE_ID_CYCLES)` is returned — the spend is accepted with no authorization whatsoever.

### Citations

**File:** script/src/types.rs (L722-725)
```rust
            let lock_group_entry = lock_groups
                .entry(output.calc_lock_hash())
                .or_insert_with(|| ScriptGroup::from_lock_script(&output.lock()));
            lock_group_entry.input_indices.push(i);
```

**File:** script/src/types.rs (L940-942)
```rust
    pub fn groups(&self) -> impl Iterator<Item = (&'_ Byte32, &'_ ScriptGroup)> {
        self.lock_groups.iter().chain(self.type_groups.iter())
    }
```

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

**File:** script/src/verify.rs (L452-453)
```rust
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
```

**File:** script/src/verify.rs (L624-625)
```rust
        if group.script.code_hash() == TYPE_ID_CODE_HASH.into()
            && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
```

**File:** script/src/type_id.rs (L52-76)
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
        Ok(TYPE_ID_CYCLES)
```
