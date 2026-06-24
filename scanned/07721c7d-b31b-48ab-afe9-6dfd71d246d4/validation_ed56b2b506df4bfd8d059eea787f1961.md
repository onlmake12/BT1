Audit Report

## Title
`DaoScriptSizeVerifier` and `CapacityVerifier` Bypass via `hash_type = "data"` DAO Type Script — (`File: verification/src/transaction_verifier.rs`)

## Summary

`cell_uses_dao_type_script` hard-codes a check for `hash_type == ScriptHashType::Type`, so any DAO cell whose type script references the DAO binary via `hash_type = "data"` (using the publicly-known `CODE_HASH_DAO` data hash) is invisible to both `DaoScriptSizeVerifier` and `CapacityVerifier`. This allows a transaction sender to submit a DAO withdrawal whose output lock script differs in serialised size from the deposit cell's lock script, bypassing the sole node-level guard against a known DAO script weakness and enabling extraction of capacity beyond what the DAO interest formula permits.

## Finding Description

`cell_uses_dao_type_script` is defined at lines 525–534 of `verification/src/transaction_verifier.rs`:

```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output
        .type_()
        .to_opt()
        .map(|t| {
            Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                && &t.code_hash() == dao_type_hash
        })
        .unwrap_or(false)
}
```

The function requires both `hash_type == ScriptHashType::Type` AND `code_hash == dao_type_hash` (the type-hash of the genesis DAO cell). A cell whose type script uses `{ code_hash: CODE_HASH_DAO, hash_type: "data" }` resolves and executes the identical DAO binary via CKB's data-hash resolution path, but this function returns `false` for it.

This function is the sole gate for two verifiers:

**`DaoScriptSizeVerifier::verify` (lines 855–858):**
```rust
if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
    && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
{
    continue;
}
```
When `false`, the verifier skips the lock-script-size equality check entirely.

**`CapacityVerifier::valid_dao_withdraw_transaction` (lines 517–522):**
```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```
When `false`, the `OutputsSumOverflow` suppression is not applied, constraining the attacker to `outputs_capacity ≤ inputs_capacity`.

The `ScriptHashTypeVerifier` (lines 796–814) only checks `output.lock().hash_type()`, not `output.type_().hash_type()`, so a type script with `hash_type = "data"` is not blocked at the non-contextual verification stage.

**Exploit flow:**
1. Attacker deposits into DAO using `{ code_hash: CODE_HASH_DAO, hash_type: "data", args: "0x" }` as the type script and a large lock script.
2. Attacker constructs a withdrawal transaction with the same `hash_type = "data"` DAO type script but a *smaller* lock script (reducing occupied capacity).
3. `cell_uses_dao_type_script` returns `false` for both cells → `DaoScriptSizeVerifier` skips the pair → lock-script-size mismatch is not caught.
4. `CapacityVerifier` applies `OutputsSumOverflow` (suppression not triggered), but since `outputs ≤ inputs`, it passes.
5. The DAO script executes normally (resolved by data hash) and validates the withdrawal without independently checking lock script sizes.
6. The transaction is accepted; the attacker's withdrawal cell has lower occupied capacity than the deposit, freeing shannons beyond the intended DAO interest.

## Impact Explanation

`DaoScriptSizeVerifier` is explicitly documented as *"a temporary solution till Nervos DAO script can be properly upgraded"* — it is the **only** consensus-enforced guard against the known DAO lock-script-size mismatch vulnerability. Bypassing it allows capacity extraction beyond what the DAO interest formula should permit. This constitutes **incorrect behavior of a system script protection mechanism**, mapping to the allowed impact: **High — Incorrect implementation or behavior of CKB-VM or system scripts (10001–15000 points)**.

## Likelihood Explanation

`CODE_HASH_DAO` is a public constant embedded in the chain spec and importable from `ckb_resource`. Any transaction sender can construct the bypass without privileged access, miner collusion, or key material. The technique requires knowledge of CKB's multi-hash-type script resolution (documented in the RFC). It is not triggered by ordinary wallet usage but is straightforwardly constructable by a motivated attacker.

## Recommendation

Extend `cell_uses_dao_type_script` to also match cells whose type script uses `hash_type = "data"` (or `"data1"` / `"data2"`) with `code_hash == CODE_HASH_DAO`. Concretely, add a second branch:

```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32, dao_data_hash: &Byte32) -> bool {
    cell_output
        .type_()
        .to_opt()
        .map(|t| {
            let ht: u8 = t.hash_type().into();
            (ht == ScriptHashType::Type as u8 && &t.code_hash() == dao_type_hash)
                || (matches!(ht, DATA | DATA1 | DATA2) && &t.code_hash() == dao_data_hash)
        })
        .unwrap_or(false)
}
```

Alternatively, resolve the script binary from the cell dep and compare the raw data hash directly, independent of the `hash_type` field.

## Proof of Concept

1. **Deposit transaction**: output with `type_script = { code_hash: CODE_HASH_DAO, hash_type: 0 /* data */, args: 0x }`, `data = 0x0000000000000000`, and a large lock script. Include the genesis DAO cell as a cell dep (by out-point). The DAO script executes and accepts the deposit.

2. **Withdrawal transaction (phase 1 → phase 2)**: input is the deposit cell above; output has the same `hash_type = "data"` DAO type script but a *smaller* lock script. Set `outputs_capacity ≤ inputs_capacity`.

3. **Node verification**:
   - `cell_uses_dao_type_script` → `false` for both → `DaoScriptSizeVerifier` skips → **no lock-script-size check**.
   - `valid_dao_withdraw_transaction` → `false` → `OutputsSumOverflow` check applied → passes because `outputs ≤ inputs`.
   - DAO script executes via data-hash resolution → validates withdrawal → passes.
   - Transaction accepted into a block.

4. **Result**: withdrawal cell has smaller occupied capacity than deposit cell; attacker recovers shannons that should have remained locked, beyond the DAO interest entitlement.