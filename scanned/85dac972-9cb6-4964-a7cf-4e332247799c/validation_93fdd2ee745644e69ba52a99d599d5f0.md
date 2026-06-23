### Title
`valid_dao_withdraw_transaction` Overly Broad Check Bypasses Capacity Conservation for DAO Phase-1 Transactions - (File: `verification/src/transaction_verifier.rs`)

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction` returns `true` for **any** transaction whose inputs contain a cell with the DAO type script — including Phase-1 (deposit→prepare) transactions. This causes `CapacityVerifier::verify()` to unconditionally skip the `OutputsSumOverflow` guard for Phase-1 transactions. The on-chain DAO script only enforces that the prepare-output capacity equals the deposit-output capacity; it does not enforce total transaction capacity conservation. A transaction sender can therefore attach extra non-DAO outputs with inflated capacity to a Phase-1 transaction and have those shannons accepted by the node, creating CKB capacity out of thin air.

### Finding Description

`CapacityVerifier::verify()` skips the `OutputsSumOverflow` check when `valid_dao_withdraw_transaction()` returns `true`:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = ...;
    let outputs_sum = ...;
    if inputs_sum < outputs_sum { return Err(...OutputsSumOverflow...); }
}
``` [1](#0-0) 

The guard function is:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

`cell_uses_dao_type_script` checks only `hash_type == Type && code_hash == dao_type_hash`: [3](#0-2) 

This predicate is `true` for **both** DAO lifecycle phases:

| Phase | Input cell data | Output | Capacity relation |
|---|---|---|---|
| Phase 1 (deposit→prepare) | 8 zero bytes | DAO prepare cell | must equal input |
| Phase 2 (prepare→regular) | non-zero block number | regular cell | may exceed input (interest) |

The comment in the test helper confirms the DAO on-chain script only enforces the per-cell-pair equality for Phase 1:

```rust
// NOTE: dao.c uses `deposit_header` to ensure the prepare_output.capacity == deposit_output.capacity
``` [4](#0-3) 

The DAO script does **not** audit the total transaction capacity balance. Therefore, if a Phase-1 transaction carries additional non-DAO outputs, neither the Rust-level guard (skipped) nor the DAO script (only checks the DAO cell pair) will catch the discrepancy.

`DaoScriptSizeVerifier` also runs for DAO transactions but only checks lock-script size parity, not capacity: [5](#0-4) 

### Impact Explanation

An attacker who owns a DAO deposit cell of capacity `X` can craft a Phase-1 transaction:

- **Input**: DAO deposit cell, capacity `X`
- **Output 0**: DAO prepare cell, capacity `X` (passes DAO script check)
- **Output 1**: Arbitrary non-DAO cell, capacity `Y` (unchecked)

Total outputs = `X + Y > X` = total inputs. Because `valid_dao_withdraw_transaction` returns `true`, the `OutputsSumOverflow` guard is skipped. The DAO script approves the DAO cell pair. Output 1 is never audited for capacity conservation. The node accepts the transaction, and `Y` shannons are created from nothing — a direct inflation of the CKB token supply.

### Likelihood Explanation

Any unprivileged transaction sender who holds a live DAO deposit cell can trigger this. No special privilege, key leak, or majority hash power is required. The attacker only needs to submit a crafted transaction via the standard RPC or P2P relay path.

### Recommendation

`valid_dao_withdraw_transaction` must distinguish Phase-1 from Phase-2 inputs. Only Phase-2 inputs (prepare cells, identified by non-zero `deposited_block_number` in their 8-byte cell data) legitimately require the capacity overflow skip. The function should be tightened to:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| {
            cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash)
                && is_dao_prepare_cell(cell_meta)   // data len == 8 && value != 0
        })
}
```

This mirrors the distinction already made in `DaoCalculator::transaction_maximum_withdraw`, which treats `deposited_block_number == 0` as a deposit cell (face value only) and `> 0` as a prepare cell (interest eligible): [6](#0-5) 

### Proof of Concept

1. Obtain a live DAO deposit cell `D` with capacity `X` (e.g., 1000 CKB).
2. Build a transaction:
   - **input[0]**: `D` (DAO type script, data = `[0u8; 8]`)
   - **output[0]**: DAO prepare cell, capacity `X`, data = current block number (8 bytes LE)
   - **output[1]**: Any lock script, no type script, capacity `Y` = 100 CKB
   - **header_deps**: deposit block hash
   - **witnesses[0]**: `WitnessArgs { input_type: Some(index_of_deposit_header) }`
3. Submit via `send_transaction` RPC.
4. `CapacityVerifier::valid_dao_withdraw_transaction` returns `true` → overflow check skipped.
5. DAO script verifies `output[0].capacity == input[0].capacity` → passes.
6. `output[1]` (100 CKB) is never audited.
7. Transaction is committed; 100 CKB created from nothing.

The existing integration test `WithdrawDAOWithOverflowCapacity` only tests Phase-2 overflow rejection and does not cover this Phase-1 vector: [7](#0-6)

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

**File:** verification/src/transaction_verifier.rs (L517-522)
```rust
    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
```

**File:** verification/src/transaction_verifier.rs (L525-534)
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

**File:** test/src/specs/dao/dao_user.rs (L96-96)
```rust
        // NOTE: dao.c uses `deposit_header` to ensure the prepare_output.capacity == deposit_output.capacity
```

**File:** util/dao/src/lib.rs (L58-116)
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
```

**File:** test/src/specs/dao/dao_tx.rs (L38-78)
```rust
pub struct WithdrawDAOWithOverflowCapacity;

impl Spec for WithdrawDAOWithOverflowCapacity {
    fn modify_chain_spec(&self, spec: &mut ckb_chain_spec::ChainSpec) {
        spec.params.genesis_epoch_length = Some(2);
        spec.params.epoch_duration_target = Some(16);
        spec.params.permanent_difficulty_in_dummy = Some(true);
    }

    fn run(&self, nodes: &mut Vec<Node>) {
        let node = &nodes[0];
        let utxos = generate_utxo_set(node, 21);
        let mut user = DAOUser::new(node, utxos);

        ensure_committed(node, &user.deposit());
        node.mine(20); // Time makes interest
        ensure_committed(node, &user.prepare());

        let withdrawal = user.withdraw();
        let invalid_withdrawal = {
            let outputs: Vec<_> = withdrawal
                .outputs()
                .into_iter()
                .map(|cell_output| {
                    let old_capacity: Capacity = cell_output.capacity().into();
                    let new_capacity = old_capacity.safe_add(Capacity::one()).unwrap();
                    cell_output.as_builder().capacity(new_capacity).build()
                })
                .collect();
            withdrawal
                .as_advanced_builder()
                .set_outputs(outputs)
                .build()
        };
        let since = EpochNumberWithFraction::from_full_value(
            withdrawal.inputs().get(0).unwrap().since().into(),
        );
        goto_target_point(node, since);
        assert_send_transaction_fail(node, &invalid_withdrawal, "Overflow");
        ensure_committed(node, &withdrawal);
    }
```
