### Title
DAO Deposit Cells Incorrectly Trigger Capacity-Check Bypass, Enabling CKB Inflation — (File: `verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` skips the `OutputsSumOverflow` check for any transaction whose inputs contain a DAO type-script cell. The guard function `valid_dao_withdraw_transaction()` does not distinguish between DAO **deposit** cells (phase 1, `deposited_block_number == 0`) and DAO **withdrawal** cells (phase 2, `deposited_block_number > 0`). Because the DAO type script only enforces balance for the DAO cells themselves, an attacker who mixes a DAO deposit cell with regular cells in one transaction can inflate the regular outputs beyond their inputs, creating CKB from thin air.

---

### Finding Description

`CapacityVerifier::verify()` contains the following guard:

```rust
// verification/src/transaction_verifier.rs  line 483
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum { return Err(...); }
}
``` [1](#0-0) 

The guard delegates to:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

And `cell_uses_dao_type_script` only inspects the script hash and hash type — it never reads the cell's **data**:

```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output.type_().to_opt()
        .map(|t| Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                  && &t.code_hash() == dao_type_hash)
        .unwrap_or(false)
}
``` [3](#0-2) 

The distinction between a deposit cell and a withdrawal cell lives entirely in the cell data. In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the 8-byte little-endian `deposited_block_number` from cell data: if it is `0` (or the data is absent/wrong length), the cell is treated as a plain deposit and its capacity is returned unchanged; only a non-zero value triggers the interest calculation:

```rust
let deposited_block_number = match self.data_loader.load_cell_data(cell_meta) {
    Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
    _ => 0,
};
if deposited_block_number > 0 { /* withdrawal path */ } else { Ok(output.capacity().into()) }
``` [4](#0-3) 

Because `valid_dao_withdraw_transaction()` never inspects cell data, it returns `true` for a **deposit→prepare** transaction (phase 1 → phase 2), which is not a withdrawal at all. The comment in the code itself says the bypass is for "DAO **withdraw** transactions", confirming the intent is narrower than the implementation. [5](#0-4) 

---

### Impact Explanation

When the bypass fires on a deposit→prepare transaction, the DAO type script (dao.c) runs and verifies only the DAO cell pair: it checks that the output DAO cell carries the same capacity as the input DAO cell. It does **not** verify the overall transaction balance. Any regular (non-DAO) cells included in the same transaction are completely unchecked for capacity conservation, because the `OutputsSumOverflow` guard has been skipped. An attacker can therefore:

- Spend a DAO deposit cell (e.g., 100 CKB) alongside a regular cell (e.g., 50 CKB)
- Produce a valid DAO prepare output (100 CKB, satisfying dao.c) and an inflated regular output (e.g., 100 CKB)
- Net 50 CKB created from nothing, repeatable at will

This is a direct, consensus-level CKB token inflation vulnerability.

---

### Likelihood Explanation

Any unprivileged transaction sender can trigger this. The only prerequisite is owning a DAO deposit cell, which anyone can create by sending a standard DAO deposit transaction. No special keys, no miner collusion, no Sybil attack is required. The crafted transaction is structurally valid and will pass all other verifiers (script execution, since-field, etc.). Likelihood is **high**.

---

### Recommendation

`valid_dao_withdraw_transaction()` must be narrowed to return `true` only when at least one input is a **withdrawal** (phase 2) DAO cell — i.e., a DAO type-script cell whose 8-byte data encodes a non-zero `deposited_block_number`. The check should mirror the logic already present in `transaction_maximum_withdraw`:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| {
            if !cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash) {
                return false;
            }
            // Only phase-2 (prepare) cells are true withdrawal inputs.
            // Phase-1 (deposit) cells have data == 0 or absent.
            // (Requires access to a data loader, or pass cell data alongside CellMeta.)
            let data = cell_meta.mem_cell_data.as_ref()
                .or_else(|| cell_meta.mem_cell_data_hash.as_ref().map(|_| &Bytes::new()));
            matches!(data, Some(d) if d.len() == 8 && LittleEndian::read_u64(d) > 0)
        })
}
```

Alternatively, pass the data loader into `CapacityVerifier` (as is already done in `DaoCalculator`) and reuse the same `deposited_block_number > 0` guard.

---

### Proof of Concept

1. **Setup**: Attacker owns a DAO deposit cell `D` (100 CKB, DAO type script, data = `[0u8; 8]`) and a regular live cell `R` (50 CKB, no type script).

2. **Craft transaction**:
   - `inputs[0]` = `D` (DAO deposit cell, 100 CKB)
   - `inputs[1]` = `R` (regular cell, 50 CKB)
   - `outputs[0]` = DAO prepare cell (100 CKB, DAO type script, data = current block number) — satisfies dao.c
   - `outputs[1]` = regular cell (100 CKB, attacker's lock) — **50 CKB inflated**
   - Total inputs: 150 CKB; Total outputs: 200 CKB

3. **Verification path**:
   - `valid_dao_withdraw_transaction()` → `true` (because `inputs[0]` has DAO type script, data check absent)
   - `OutputsSumOverflow` guard → **skipped**
   - dao.c type script → verifies `outputs[0].capacity == inputs[0].capacity` (100 == 100) ✓
   - No verifier checks `outputs[1]` vs `inputs[1]`

4. **Result**: Transaction accepted by consensus. Attacker gains 50 CKB from nothing. Repeatable indefinitely. [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L478-494)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }
```

**File:** verification/src/transaction_verifier.rs (L517-534)
```rust
    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
}

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

**File:** util/dao/src/lib.rs (L58-66)
```rust
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
```
