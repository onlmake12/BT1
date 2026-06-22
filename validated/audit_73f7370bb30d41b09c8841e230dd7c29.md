### Title
`DaoScriptSizeVerifier` Protection Bypass via Alternative Script Hash Type — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

The `cell_uses_dao_type_script` helper function, used by both `CapacityVerifier` and `DaoScriptSizeVerifier`, only recognizes DAO cells whose type script uses `hash_type = "type"`. Because the same DAO script binary is also reachable via `hash_type = "data"` (using the publicly-known `CODE_HASH_DAO` data hash), a transaction sender can construct a DAO cell that executes the real DAO script yet is invisible to both verifiers — directly analogous to the M-02 "multiple entry points" bypass.

---

### Finding Description

`cell_uses_dao_type_script` is defined as:

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

It hard-codes the requirement that `hash_type == ScriptHashType::Type`. The `dao_type_hash` it compares against is the *type-hash* of the DAO cell (i.e., the hash of the type script of the genesis cell that holds the DAO binary).

However, CKB supports four hash types for script resolution: `Data`, `Data1`, `Data2`, and `Type`. A script can be referenced by its *data hash* (`CODE_HASH_DAO`, the Blake2b hash of the DAO binary itself) with `hash_type = "data"`. Both references resolve to and execute the identical DAO bytecode, but only the `hash_type = "type"` form is recognised by `cell_uses_dao_type_script`.

This function is the sole gate for two consensus-enforced verifiers:

1. **`DaoScriptSizeVerifier::verify`** — enforces that DAO deposit and withdrawal cells use lock scripts of identical serialised size (a temporary mitigation for a known DAO script vulnerability). When `cell_uses_dao_type_script` returns `false`, the verifier skips the pair entirely.

2. **`CapacityVerifier::valid_dao_withdraw_transaction`** — suppresses the `OutputsSumOverflow` check for DAO withdrawals (because interest legitimately inflates outputs). When `cell_uses_dao_type_script` returns `false`, the suppression is not applied.

A transaction sender can craft a DAO deposit cell with:

```
type_script = { code_hash: CODE_HASH_DAO, hash_type: "data", args: "0x" }
```

The DAO script binary executes normally (the VM resolves it by data hash), but `cell_uses_dao_type_script` returns `false` for every verifier call, so `DaoScriptSizeVerifier` silently skips the lock-script-size enforcement for that cell pair.

---

### Impact Explanation

`DaoScriptSizeVerifier` is documented as *"a temporary solution till Nervos DAO script can be properly upgraded"* — meaning it is the **only** node/consensus-level guard against a known DAO script weakness involving mismatched lock script sizes between deposit and withdrawal cells. Bypassing it allows a transaction sender to submit a DAO withdrawal whose output lock script differs in size from the deposit cell's lock script, a condition the DAO script itself does not independently reject. This can be exploited to manipulate occupied-capacity accounting in the withdrawal output relative to the deposit, potentially extracting capacity beyond what the DAO interest formula should permit.

The `CapacityVerifier` side-effect (re-enabling `OutputsSumOverflow` for the `hash_type = "data"` path) constrains the attacker to scenarios where `outputs_capacity ≤ inputs_capacity`, but this is still sufficient to exploit the lock-script-size mismatch because the attacker can choose a *smaller* withdrawal lock script, reducing occupied capacity and freeing shannons that were locked in the deposit.

**Impact: Medium** — bypasses a consensus-enforced protection for the Nervos DAO, potentially enabling capacity extraction beyond the intended interest.

---

### Likelihood Explanation

`CODE_HASH_DAO` is a public constant embedded in the chain spec. Any transaction sender can construct the bypass without any privileged access. The technique requires knowledge of CKB's multi-hash-type script resolution, which is documented in the RFC. No key material, miner collusion, or Sybil attack is needed.

**Likelihood: Low** — requires deliberate construction of a non-standard DAO cell; not triggered by ordinary wallet usage.

---

### Recommendation

Extend `cell_uses_dao_type_script` to also match cells whose type script uses `hash_type = "data"` (or `"data1"` / `"data2"`) with `code_hash == CODE_HASH_DAO` (the DAO binary data hash). Alternatively, resolve the script binary from the cell dep and compare the raw data hash directly, independent of the `hash_type` field, so that all referencing forms of the same DAO binary are covered.

---

### Proof of Concept

1. **Deposit**: submit a transaction whose output has:
   ```
   type_script = { code_hash: <CODE_HASH_DAO>, hash_type: "data", args: "0x" }
   data: 0x0000000000000000   // 8 zero bytes — valid DAO deposit marker
   lock: <small_lock>         // e.g., secp256k1 with 20-byte args
   ```
   The DAO script executes (resolved by data hash) and accepts the deposit.

2. **Withdrawal (phase 1 → phase 2)**: construct the withdrawal transaction with:
   ```
   output type_script = { code_hash: <CODE_HASH_DAO>, hash_type: "data", args: "0x" }
   lock: <large_lock>         // e.g., multisig with 28-byte args — different size
   outputs_capacity ≤ inputs_capacity
   ```

3. **Node-side verification**:
   - `cell_uses_dao_type_script` returns `false` for both input and output → `DaoScriptSizeVerifier` skips the pair → **lock-script-size mismatch is not caught**.
   - `valid_dao_withdraw_transaction` returns `false` → `CapacityVerifier` applies `OutputsSumOverflow` → passes because `outputs ≤ inputs`.
   - The DAO script itself executes and validates the withdrawal.
   - The transaction is accepted into a block by all nodes.

The `DaoScriptSizeVerifier` protection is fully bypassed for any DAO cell constructed with `hash_type = "data"`.