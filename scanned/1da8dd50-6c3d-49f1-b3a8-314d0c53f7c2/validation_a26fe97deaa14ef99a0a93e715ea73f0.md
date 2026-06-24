Audit Report

## Title
TYPE_ID System Script Unconditionally Succeeds as Lock Script for Single-Input Spends — (`script/src/type_id.rs`)

## Summary
`TypeIdSystemScript::verify()` contains no `group_type` guard and no authentication logic for the case where `input_indices` is non-empty and `output_indices` is empty. Because lock script groups structurally always satisfy this condition, any cell whose `lock` field references `TYPE_ID_CODE_HASH` with `hash_type: Type` and 32-byte args can be spent by an unprivileged attacker in a single-input, witness-free transaction. The TYPE_ID system script returns `Ok(TYPE_ID_CYCLES)` without verifying any signature or preimage.

## Finding Description
`verify_script_group` in `script/src/verify.rs` (L427–444) dispatches to `TypeIdSystemScript` whenever `code_hash == TYPE_ID_CODE_HASH && hash_type == Type`, with no check on `group.group_type`. [1](#0-0) 

Script group construction in `script/src/types.rs` (L716–740) only ever appends to `output_indices` for type script groups (the second loop, L733–740). Lock script groups are built exclusively in the first loop and only receive `input_indices` entries. A lock group's `output_indices` is therefore always empty. [2](#0-1) 

Inside `TypeIdSystemScript::verify()` (`script/src/type_id.rs` L29–77), the execution path for a lock group with one input is:

1. `max_cycles < TYPE_ID_CYCLES` → false, continues. [3](#0-2) 
2. `args().len() != 32` → false (attacker supplies 32-byte args), continues. [4](#0-3) 
3. `input_indices.len() > 1 || output_indices.len() > 1` → `1 > 1 || 0 > 1` → false, continues. [5](#0-4) 
4. `input_indices.is_empty()` → false (one input present) → the only cryptographic check (creation hash validation) is **skipped entirely**. [6](#0-5) 
5. Falls through to `Ok(TYPE_ID_CYCLES)` — no authentication performed. [7](#0-6) 

The root cause is that the TYPE_ID verifier was designed only for the type script role (creation vs. transfer semantics) and has no concept of being invoked as a lock script. The dispatch in `verify_script_group` does not restrict invocation to type script groups, and `TypeIdSystemScript::verify()` itself has no `group_type` check.

## Impact Explanation
This is an **incorrect implementation of a CKB system script** (High, 10001–15000 points). Any cell locked with TYPE_ID as a lock script can be stolen by an unprivileged attacker with a trivial, witness-free transaction. The attacker redirects the cell's full CKB capacity to an output they control. If such cells exist on-chain (e.g., deployed by a developer who misunderstood TYPE_ID's intended role), the economic loss is direct and immediate, potentially elevating impact to "damage CKB economy" (Critical).

## Likelihood Explanation
The attacker requires no special privileges — only the existence of a cell whose `lock` field uses `TYPE_ID_CODE_HASH` with `hash_type: Type` and 32-byte args. While this is an atypical configuration, the protocol does not prevent it. A developer who conflates TYPE_ID's uniqueness guarantee with a lock mechanism could deploy such a cell. Once on-chain, any observer can exploit it with a single transaction and no witness. The exploit is deterministic and repeatable.

## Recommendation
Add a `group_type` guard at the entry of `TypeIdSystemScript::verify()` (or equivalently in `verify_script_group` before dispatching):

```rust
// In TypeIdSystemScript::verify(), at the top:
if self.script_group.group_type == ScriptGroupType::Lock {
    return Err(self.validation_failure(ERROR_ARGS));
}
```

Alternatively, restrict the dispatch in `verify_script_group` to type script groups only:

```rust
if group.group_type == ScriptGroupType::Type
    && group.script.code_hash() == TYPE_ID_CODE_HASH.into()
    && Into::<u8>::into(group.script.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
{
    // dispatch to TypeIdSystemScript
}
```

## Proof of Concept
```
1. Create cell C on-chain:
     lock.code_hash = TYPE_ID_CODE_HASH
     lock.hash_type = Type
     lock.args      = [0u8; 32]   // any 32-byte value

2. Build transaction T:
     inputs   = [OutPoint(C)]
     outputs  = [attacker_cell with attacker's lock]
     witnesses = []               // empty — no signature required

3. Submit T to the network.

4. verify_script_group is called for C's lock group:
     group.group_type     = Lock
     group.input_indices  = [0]
     group.output_indices = []
     → dispatched to TypeIdSystemScript::verify()
     → all guards pass, returns Ok(TYPE_ID_CYCLES)

5. T is accepted; attacker receives C's full CKB capacity.
```

A unit test can be written in `script/src/tests/` by constructing a `ResolvedTransaction` with a single input cell carrying the TYPE_ID lock, an empty witness, and verifying that `TransactionScriptsVerifier::verify()` currently returns `Ok` — confirming the bypass.

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

**File:** script/src/type_id.rs (L30-32)
```rust
        if self.max_cycles < TYPE_ID_CYCLES {
            return Err(ScriptError::ExceededMaximumCycles(self.max_cycles));
        }
```

**File:** script/src/type_id.rs (L36-38)
```rust
        if self.script_group.script.args().len() != 32 {
            return Err(self.validation_failure(ERROR_ARGS));
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

**File:** script/src/type_id.rs (L76-77)
```rust
        Ok(TYPE_ID_CYCLES)
    }
```
