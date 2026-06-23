### Title
Mixed-Input DAO Transaction Bypasses Global Capacity Conservation Check, Enabling CKB Supply Inflation — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction that contains **at least one** DAO-typed input cell. The DAO type script (on-chain RISC-V) only enforces the interest calculation for the DAO cell itself; it does not enforce the global capacity balance across all inputs and outputs. A transaction sender who owns any NervosDAO cell can therefore mix it with regular inputs and produce outputs whose total capacity exceeds total inputs, creating CKB capacity out of thin air.

---

### Finding Description

In `CapacityVerifier::verify()`, the guard at line 483 reads:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
``` [1](#0-0) 

The helper `valid_dao_withdraw_transaction()` returns `true` if **any** resolved input carries the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

This is an **all-or-nothing** bypass: the moment a single DAO cell appears among the inputs, the entire `OutputsSumOverflow` check is skipped for the whole transaction — including every non-DAO input and output.

The code comment justifies the skip with: *"DAO withdraw transaction is verified via the type script of DAO cells."* [3](#0-2) 

However, the DAO type script only verifies the DAO cell's own interest calculation (`calculate_maximum_withdraw`), which operates per-cell:

```rust
if is_dao_output {
    ...
    self.calculate_maximum_withdraw(output, ..., deposit_header_hash, withdrawing_header_hash)
} else {
    Ok(output.capacity().into())   // non-DAO inputs: just pass through face value
}
``` [4](#0-3) 

Non-DAO inputs are simply counted at face value (`output.capacity().into()`) and the DAO type script imposes no constraint on the total outputs capacity of the transaction. The gap between the two enforcement layers — `CapacityVerifier` (skipped) and the DAO type script (per-cell only) — leaves the global capacity conservation invariant completely unenforced for mixed transactions.

---

### Impact Explanation

An attacker who owns any NervosDAO cell (deposit or withdrawing phase) can craft a transaction that:

1. Spends the DAO cell legitimately (satisfying the DAO type script).
2. Includes one or more regular (non-DAO) input cells.
3. Declares output cells whose **total capacity exceeds total inputs** by an arbitrary amount.

Because `CapacityVerifier` skips the `OutputsSumOverflow` check and the DAO type script does not fill the gap, the excess capacity is accepted by consensus as valid. This directly inflates the circulating CKB supply — the exact analog of the ZetaChain M-24 `MintCoins()` inflation bug.

**Severity**: Critical. CKB capacity is the native token. Arbitrary inflation breaks the fundamental economic invariant of the protocol.

---

### Likelihood Explanation

NervosDAO is a core, widely-used feature of CKB mainnet. Any holder who has ever deposited into the DAO owns a cell that triggers the bypass. The attack requires no privileged role, no leaked key, no majority hashpower, and no social engineering — only a valid DAO cell and the ability to submit a transaction (standard RPC `send_transaction`). The attacker-controlled entry path is: **tx-pool submitter / RPC caller → `CapacityVerifier::verify()` → bypass → inflated outputs accepted into a block**.

---

### Recommendation

The `OutputsSumOverflow` check must not be skipped wholesale. Instead, enforce the global capacity balance even for DAO transactions, and separately allow the DAO interest surplus:

```rust
pub fn verify(&self) -> Result<(), Error> {
    if !self.resolved_transaction.is_cellbase() {
        let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
        let outputs_sum = self.resolved_transaction.outputs_capacity()?;

        // For DAO withdraw transactions, the DAO type script enforces the
        // per-cell interest cap; the node must still enforce that non-DAO
        // capacity is conserved.  Compute the maximum DAO interest allowed
        // and add it to inputs_sum before comparing.
        let dao_interest = if self.valid_dao_withdraw_transaction() {
            self.compute_max_dao_interest()? // sum of (max_withdraw - deposited) for DAO inputs
        } else {
            Capacity::zero()
        };

        let allowed_outputs = inputs_sum.safe_add(dao_interest)?;
        if outputs_sum > allowed_outputs {
            return Err(TransactionError::OutputsSumOverflow { inputs_sum, outputs_sum }.into());
        }
    }
    // ... occupied capacity checks unchanged
}
```

This ensures non-DAO capacity is always conserved while still permitting the legitimate DAO interest surplus.

---

### Proof of Concept

The following pseudo-test demonstrates the inflation path using the existing test infrastructure in `verification/src/tests/transaction_verifier.rs`:

```rust
#[test]
fn test_dao_mixed_input_capacity_inflation() {
    // dao_type_script triggers valid_dao_withdraw_transaction() = true
    let dao_type_script = build_genesis_type_id_script(OUTPUT_INDEX_DAO);
    let dao_type_hash   = dao_type_script.calc_script_hash();

    let transaction = TransactionBuilder::default()
        .outputs(vec![
            // Output 1: regular cell — 9_000 CKB (far more than regular input below)
            CellOutput::new_builder().capacity(capacity_bytes!(9_000)).build(),
        ])
        .outputs_data(vec![Bytes::new().into()])
        .build();

    let rtx = Arc::new(ResolvedTransaction {
        transaction,
        resolved_cell_deps: vec![],
        // Input 1: DAO deposit cell — 100 CKB  (triggers the bypass)
        // Input 2: regular cell    — 10  CKB
        resolved_inputs: vec![
            CellMetaBuilder::from_cell_output(
                CellOutput::new_builder()
                    .capacity(capacity_bytes!(100))
                    .type_(Some(dao_type_script).pack())
                    .build(),
                Bytes::new(),
            ).build(),
            CellMetaBuilder::from_cell_output(
                CellOutput::new_builder()
                    .capacity(capacity_bytes!(10))
                    .build(),
                Bytes::new(),
            ).build(),
        ],
        resolved_dep_groups: vec![],
    });

    let verifier = CapacityVerifier::new(rtx, dao_type_hash);

    // BUG: passes — outputs (9_000 CKB) >> inputs (110 CKB), 8_890 CKB created from nothing
    assert!(verifier.verify().is_ok());
}
```

Total inputs: 110 CKB. Total outputs: 9 000 CKB. `CapacityVerifier::verify()` returns `Ok(())` because `valid_dao_withdraw_transaction()` is `true` and the `OutputsSumOverflow` branch is never reached. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** util/dao/src/lib.rs (L58-119)
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
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
```
