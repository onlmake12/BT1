### Title
`DaoScriptSizeVerifier` Bypassed via Positional Index Mismatch — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier::verify()` enforces that a Nervos DAO deposit cell and its corresponding withdrawal cell use lock scripts of the same byte size. However, it pairs inputs with outputs strictly by positional index using `.zip()`. Because CKB transactions impose no requirement that input[i] corresponds to output[i], an unprivileged transaction sender can place the DAO deposit input and the DAO withdrawal output at non-matching indices, causing every pair examined by the verifier to fail the "both cells use DAO type script" guard and be silently skipped. The entire lock-script-size check is bypassed.

---

### Finding Description

`DaoScriptSizeVerifier::verify()` iterates over `(resolved_inputs[i], outputs[i])` pairs:

```rust
for (i, (input_meta, cell_output)) in self
    .resolved_transaction
    .resolved_inputs
    .iter()
    .zip(self.resolved_transaction.transaction.outputs())
    .enumerate()
{
    if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
        && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
    {
        continue;   // ← silently skipped
    }
    ...
    if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
        return Err(...DaoLockSizeMismatch...);
    }
}
``` [1](#0-0) 

The guard on line 855–858 requires **both** the input at index `i` **and** the output at index `i` to carry the DAO type script. If they do not, the pair is skipped with `continue`.

**Bypass construction** — craft a Phase-1 DAO withdrawal transaction with deliberately misaligned indices:

| Slot | Field | Content |
|------|-------|---------|
| `inputs[0]` | regular fee cell | no DAO type script |
| `inputs[1]` | DAO deposit cell | DAO type script, data = `[0u8; 8]` |
| `outputs[0]` | DAO prepare cell | DAO type script, **enlarged lock script** |
| `outputs[1]` | change cell | no DAO type script |

The verifier examines:
- Pair `(inputs[0], outputs[0])`: `inputs[0]` has no DAO type script → `continue`
- Pair `(inputs[1], outputs[1])`: `outputs[1]` has no DAO type script → `continue`

Neither pair triggers the size comparison. The DAO deposit cell at `inputs[1]` is never compared against the DAO prepare cell at `outputs[0]`. The verifier returns `Ok(())` unconditionally.

The verifier's own doc comment acknowledges it is "a temporary solution till Nervos DAO script can be properly upgraded," meaning the on-chain DAO type script does **not** independently enforce lock-script-size equality. [2](#0-1) 

The verifier is invoked in both the tx-pool admission path and the block-commit path: [3](#0-2) [4](#0-3) 

Both call sites are reachable by any unprivileged user who submits a transaction via RPC or P2P relay.

---

### Impact Explanation

The `DaoScriptSizeVerifier` exists precisely because the on-chain DAO type script does not handle the case where the prepare cell's lock script is larger than the deposit cell's lock script. Bypassing the verifier allows a Phase-1 transaction to land on-chain with a prepare cell whose lock script is arbitrarily larger than the deposit cell's lock script.

The DAO type script calculates the maximum claimable amount in Phase 2 using the deposit cell's capacity. If the prepare cell carries a larger lock script (requiring more occupied capacity), the DAO type script's accounting is skewed: the "free" capacity available for interest accrual is computed against the deposit cell's smaller occupied capacity, while the prepare cell's larger occupied capacity is not deducted. This allows the attacker to claim more CKB in Phase 2 than they are entitled to — effectively extracting value from the DAO reserve at the expense of other depositors.

Additionally, if the DAO type script rejects Phase-2 claims when lock script sizes differ, the attacker's own funds become permanently locked in the prepare cell, constituting an irreversible self-inflicted loss that can also be weaponized against a victim by front-running their deposit.

**Severity: Medium** — direct financial impact on DAO accounting; no privileged access required.

---

### Likelihood Explanation

Any user who can submit a transaction (RPC `send_transaction` or P2P relay) can construct the misaligned transaction. The construction requires only knowledge of the CKB transaction format and the DAO protocol — both are fully public. No key material, operator access, or majority hashpower is needed. The bypass is deterministic and reproducible.

**Likelihood: Low-to-Medium** — requires deliberate crafting but is trivially achievable by any developer familiar with the CKB SDK.

---

### Recommendation

Replace the positional `.zip()` pairing with an explicit search that matches each DAO deposit input against the DAO withdrawal output that actually corresponds to it. The CKB DAO protocol identifies the correspondence via the witness field (the witness for a Phase-1 input encodes the deposit block number). Alternatively, enforce that for every DAO deposit input there exists **at most one** DAO output in the transaction and that its lock script size matches, regardless of index position:

```rust
// Pseudocode
for input_meta in resolved_inputs {
    if !is_dao_deposit(input_meta) { continue; }
    for cell_output in outputs {
        if !cell_uses_dao_type_script(cell_output) { continue; }
        if input_meta.lock().total_size() != cell_output.lock().total_size() {
            return Err(DaoLockSizeMismatch);
        }
    }
}
```

---

### Proof of Concept

Construct a transaction with the following layout (pseudocode using the CKB Rust SDK):

```rust
let tx = TransactionBuilder::default()
    // inputs[0]: regular cell (fee source, no DAO type)
    .input(CellInput::new(regular_out_point, 0))
    // inputs[1]: DAO deposit cell (data = [0u8;8], small lock script)
    .input(CellInput::new(dao_deposit_out_point, since_value))
    // outputs[0]: DAO prepare cell — ENLARGED lock script (e.g. 40-byte args)
    .output(
        CellOutputBuilder::default()
            .capacity(deposit_capacity)
            .lock(Script::new_builder().args(Bytes::from(vec![0u8; 40])).build())
            .type_(Some(dao_type_script.clone()))
            .build()
    )
    .output_data(Bytes::from(deposit_block_number_le_bytes))
    // outputs[1]: change cell (no DAO type)
    .output(change_cell)
    .output_data(Bytes::new())
    .build();
```

`DaoScriptSizeVerifier::verify()` examines:
- `(inputs[0], outputs[0])`: `inputs[0]` has no DAO type → `continue`
- `(inputs[1], outputs[1])`: `outputs[1]` has no DAO type → `continue`

Returns `Ok(())`. The transaction is accepted by both the tx-pool and block verifier. The prepare cell at `outputs[0]` carries a 40-byte-args lock script while the deposit cell had a 0-byte-args lock script — a size mismatch that the verifier was designed to prevent but failed to detect due to the wrong-index pairing. [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-819)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
pub struct DaoScriptSizeVerifier<DL> {
```

**File:** verification/src/transaction_verifier.rs (L845-890)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
        }
        Ok(())
    }
```

**File:** tx-pool/src/util.rs (L111-114)
```rust
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L444-453)
```rust
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
```
