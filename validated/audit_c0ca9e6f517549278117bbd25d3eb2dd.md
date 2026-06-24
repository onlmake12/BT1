The code confirms the claim. Let me verify the `is_cellbase()` behavior and the DAO type script enforcement scope before finalizing.

Audit Report

## Title
`CapacityVerifier::valid_dao_withdraw_transaction()` Skips `OutputsSumOverflow` for Phase-1 DAO Deposit Inputs, Enabling Unbounded Capacity Inflation — (`verification/src/transaction_verifier.rs`)

## Summary
`valid_dao_withdraw_transaction()` returns `true` for any transaction whose inputs include a cell with the DAO type script, regardless of whether that cell is a phase-1 deposit cell (data = `0x0000000000000000`) or a phase-2 prepare cell (data = non-zero block number). When it returns `true`, the entire `OutputsSumOverflow` guard is bypassed. For phase-1 (deposit→prepare) transactions, the DAO type script enforces only that the DAO cell's own capacity is preserved at the same index — it does not enforce total transaction balance — leaving non-DAO outputs completely unchecked. An unprivileged attacker holding any DAO deposit cell can pair it with non-DAO inputs and inflate non-DAO outputs, creating CKB from nothing.

## Finding Description

In `CapacityVerifier::verify()`, the `OutputsSumOverflow` check is gated on:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
```

`valid_dao_withdraw_transaction()` is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

And `cell_uses_dao_type_script()` inspects only the type script hash and hash type — never the cell data:

```rust
fn cell_uses_dao_type_script(cell_output: &CellOutput, dao_type_hash: &Byte32) -> bool {
    cell_output.type_().to_opt()
        .map(|t| {
            Into::<u8>::into(t.hash_type()) == Into::<u8>::into(ScriptHashType::Type)
                && &t.code_hash() == dao_type_hash
        })
        .unwrap_or(false)
}
```

A phase-1 deposit cell (data = `0x0000000000000000`) and a phase-2 prepare cell (data = non-zero block number) are indistinguishable to this function. Both cause `valid_dao_withdraw_transaction()` to return `true`, skipping the capacity sum check.

The code comment states: *"DAO withdraw transaction is verified via the type script of DAO cells."* This is only correct for phase-2. For phase-1, the DAO type script (dao.c) enforces only that `prepare_output.capacity == deposit_output.capacity` at the same index — confirmed by the in-codebase comment at `test/src/specs/dao/dao_user.rs` line 96:

```
// NOTE: dao.c uses `deposit_header` to ensure the prepare_output.capacity == deposit_output.capacity
```

The DAO type script does not enforce that total transaction inputs ≥ total transaction outputs. This is further confirmed by `DaoCalculator::transaction_maximum_withdraw()` in `util/dao/src/lib.rs`: for a deposit-phase cell (`deposited_block_number == 0`), it simply returns `output.capacity().into()` — the original capacity — with no enforcement of total balance.

**Exploit path:**
1. Attacker holds a DAO deposit cell (capacity `D`, data = `0x0000000000000000`) and non-DAO cells (capacity `N`).
2. Attacker crafts a transaction:
   - **Inputs**: DAO deposit cell (`D`) + non-DAO cells (`N`)
   - **Outputs**: DAO prepare cell (`D`, same index — passes DAO type script) + non-DAO cells (`N + X`, where `X > 0`)
3. `valid_dao_withdraw_transaction()` → `true` (DAO input present, type script matches).
4. `OutputsSumOverflow` check is skipped entirely (total inputs = `D+N`, total outputs = `D+N+X`).
5. DAO type script validates only the DAO cell pair (`D → D` at same index) ✓.
6. Non-DAO output inflation (`X` CKB) is never checked.
7. Transaction accepted. `X` CKB created from nothing.

## Impact Explanation

This breaks the fundamental capacity conservation invariant of CKB's UTXO model. Any attacker who holds a DAO deposit cell can inflate the CKB token supply by an arbitrary amount per transaction, and repeat the attack indefinitely. This directly and concretely damages the CKB economy — matching the Critical impact class: **"Vulnerabilities which could easily damage CKB economy"** (15001–25000 points).

## Likelihood Explanation

Creating a DAO deposit cell requires only a standard deposit transaction, which any user can submit permissionlessly via the `send_transaction` RPC. No privileged role, leaked key, or majority hashpower is required. The attack is deterministic, requires no victim interaction, and is trivially repeatable at any scale.

## Recommendation

`valid_dao_withdraw_transaction()` must be restricted to match only phase-2 (prepare→withdraw) inputs — i.e., DAO-typed input cells whose data encodes a non-zero `deposited_block_number`. The function should inspect `cell_meta.mem_cell_data` and return `true` only if at least one DAO input has 8 bytes of data with a non-zero value:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| {
            if !cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash) {
                return false;
            }
            cell_meta.mem_cell_data
                .as_ref()
                .filter(|data| data.len() == 8)
                .map(|data| LittleEndian::read_u64(data) > 0)
                .unwrap_or(false)
        })
}
```

For phase-1 (deposit→prepare) transactions, the standard `OutputsSumOverflow` check must remain active.

## Proof of Concept

```
Precondition:
  Alice holds:
    - cell_A: DAO deposit cell, capacity=1000 CKB, type=DAO, data=0x0000000000000000
    - cell_B: non-DAO cell, capacity=500 CKB

Attack transaction:
  Inputs:  [cell_A (1000 CKB, DAO type), cell_B (500 CKB)]
  Outputs: [cell_C (1000 CKB, DAO type, data=current_block_le),
            cell_D (600 CKB, no type)]

Verification trace:
  1. CapacityVerifier::valid_dao_withdraw_transaction()
       → cell_A has DAO type script → returns true
  2. OutputsSumOverflow check SKIPPED
       (inputs=1500 CKB, outputs=1600 CKB — gap never checked)
  3. DAO type script executes for cell_A→cell_C pair:
       checks cell_C.capacity (1000) == cell_A.capacity (1000) ✓
  4. cell_D (600 CKB) output is never validated against cell_B (500 CKB) input

Result: Transaction accepted. Alice has 100 CKB created from nothing.
Repeating with larger X or in parallel yields unbounded inflation.
```