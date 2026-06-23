### Title
DAO Withdrawal Path Bypasses `OutputsSumOverflow` Check for Entire Mixed Transaction, Enabling Miner Capacity Inflation — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` uses `.any()` to detect whether *any* resolved input carries the DAO type script. When it returns `true`, the entire `OutputsSumOverflow` guard is skipped for the whole transaction — including any regular (non-DAO) cells that are co-inputs. Because the on-chain DAO type script only validates the DAO cell's withdrawal amount and not the capacity balance of co-resident regular cells, a miner who assembles a block directly (bypassing the tx-pool) can include a transaction that mixes a legitimate DAO withdrawal with inflated regular-cell outputs, creating CKB capacity out of thin air.

---

### Finding Description

**Root cause — `CapacityVerifier::verify()`**

```
verification/src/transaction_verifier.rs  lines 478–494
```

```rust
pub fn verify(&self) -> Result<(), Error> {
    // skip OutputsSumOverflow verification for resolved cellbase and DAO
    // withdraw transactions.
    // cellbase's outputs are verified by RewardVerifier
    // DAO withdraw transaction is verified via the type script of DAO cells
    if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
        let inputs_sum  = self.resolved_transaction.inputs_capacity()?;
        let outputs_sum = self.resolved_transaction.outputs_capacity()?;
        if inputs_sum < outputs_sum {
            return Err((TransactionError::OutputsSumOverflow { … }).into());
        }
    }
    …
}
``` [1](#0-0) 

**The "neutral path" — `valid_dao_withdraw_transaction()`**

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The guard fires on `.any()`: a single DAO-typed input among many is sufficient to suppress the `OutputsSumOverflow` check for the **entire** transaction, including all co-resident regular cells.

**What the DAO type script actually validates**

`DaoCalculator::transaction_maximum_withdraw()` (used by the tx-pool fee check) correctly accounts for all inputs:

```rust
if is_dao_output {
    if deposited_block_number > 0 {
        // interest-adjusted withdrawal
        self.calculate_maximum_withdraw(…)
    } else {
        Ok(output.capacity().into())   // deposit cell → face value only
    }
} else {
    Ok(output.capacity().into())       // regular cell → face value only
}
``` [3](#0-2) 

The on-chain DAO type script (CKB-VM) validates only the DAO cell's withdrawal amount. It does not validate the capacity balance of regular cells that happen to share the same transaction. The comment in `CapacityVerifier` ("DAO withdraw transaction is verified via the type script of DAO cells") is therefore incomplete: the type script covers DAO cells only, not the whole transaction.

**Attack construction**

A miner assembles a block directly (no tx-pool path required):

| | Cell | Capacity |
|---|---|---|
| Input 1 | DAO withdrawing cell (phase-2) | 100 CKB, max-withdraw = 101 CKB |
| Input 2 | Regular cell | 50 CKB |
| Output 1 | Regular cell (DAO proceeds) | 101 CKB ✓ (DAO type script validates this) |
| Output 2 | Regular cell | **55 CKB** ← inflated by 5 CKB |

- `valid_dao_withdraw_transaction()` → `true` (Input 1 has DAO type script)
- `OutputsSumOverflow` check → **skipped**
- DAO type script → validates Output 1 = 101 CKB → **passes**
- Output 2 inflation (5 CKB) → **no check catches it**
- Net capacity created: 5 CKB per block inclusion

---

### Impact Explanation

A miner can inflate the total CKB supply by including mixed DAO+regular transactions in self-mined blocks. Each such block permanently increases the circulating capacity beyond the protocol-defined issuance schedule. This undermines CKB's core economic invariant (capacity = state storage rights) and the secondary issuance / NervosDAO interest model, which depends on accurate accounting of total locked and circulating capacity.

---

### Likelihood Explanation

Any miner — regardless of hashpower share — can exploit this on every block they successfully mine. No 51% attack, no key compromise, and no social engineering is required. The miner simply constructs the block payload directly, bypassing the tx-pool (which *would* reject the transaction via `check_tx_fee` / `DaoCalculator::transaction_fee`). [4](#0-3) 

The tx-pool guard is not a consensus rule; it is a local admission policy. Block verification is the consensus boundary, and it is where the bypass lives.

---

### Recommendation

Replace the `.any()` predicate in `valid_dao_withdraw_transaction()` with a check that is scoped to the DAO cells only. Two complementary fixes:

1. **Per-cell capacity check**: Instead of skipping `OutputsSumOverflow` for the whole transaction, skip it only for the *DAO-cell portion* of the inputs/outputs and enforce the normal conservation rule for all non-DAO cells in the same transaction.

2. **Tighten the predicate**: Only suppress the check when *all* inputs are DAO withdrawing cells (i.e., `deposited_block_number > 0`), not when any single input carries the DAO type script. A deposit cell (phase 1, `deposited_block_number == 0`) does not produce interest and should not exempt co-resident regular cells from the conservation check.

---

### Proof of Concept

**Relevant code path (block verification)**

1. Block arrives → `ContextualBlockVerifier` → `ContextualTransactionVerifier` → `CapacityVerifier::verify()`
2. Transaction has Input 1 (DAO withdrawing cell) + Input 2 (regular cell, 50 CKB) + Output 2 (regular cell, 55 CKB)
3. `valid_dao_withdraw_transaction()` returns `true` → `OutputsSumOverflow` guard is skipped
4. Script verifier runs DAO type script → validates DAO cell withdrawal amount only → passes
5. Output 2's 5 CKB inflation is never checked → block accepted by all verifying nodes

**Tx-pool path (shows the bypass is block-level only)**

`check_tx_fee` → `DaoCalculator::transaction_fee()` → `maximum_withdraw - outputs_capacity` = 151 − 156 = −5 CKB → `safe_sub` returns `Err` → transaction rejected by tx-pool [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** verification/src/transaction_verifier.rs (L517-533)
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
```

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
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

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
```
