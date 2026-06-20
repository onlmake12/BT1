### Title
`DaoCalculator::transaction_maximum_withdraw` Misclassifies Genesis-Block DAO Withdrawal Cells as Deposit Cells — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` uses `if deposited_block_number > 0` to distinguish DAO withdrawal cells (data = deposit block number) from deposit cells (data = 8 zero bytes). When the original deposit was in block 0 (the genesis block), the withdrawal cell's data encodes `0u64` in little-endian — identical to a deposit cell's sentinel. The check silently falls through to the deposit-cell branch, returning only the raw capacity with no interest. This causes `transaction_fee` to undercount the maximum withdraw, making every valid phase-2 withdrawal of a genesis-block DAO deposit fail fee validation and be rejected from the tx-pool as malformed.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the 8-byte cell data of a DAO-typed input and interprets it as the block number of the original deposit:

```rust
let deposited_block_number =
    match self.data_loader.load_cell_data(cell_meta) {
        Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
        _ => 0,
    };
if deposited_block_number > 0 {          // ← withdrawal path
    // ... full interest calculation
} else {
    Ok(output.capacity().into())         // ← deposit path (no interest)
}
``` [1](#0-0) 

The comment on line 59 states: *"A withdrawing DAO cell has 8 bytes of cell data storing the block number of the original deposit."* For a deposit cell the data is all zeros; for a withdrawal cell the data is the deposit block number encoded as `u64` LE.

The sentinel value `0` is also a valid block number — the genesis block. If a DAO deposit was made in block 0, the phase-1 withdrawal cell's data is `0x0000000000000000`, which is byte-for-byte identical to a deposit cell. The guard `deposited_block_number > 0` therefore misclassifies the withdrawal cell as a deposit cell and returns only `output.capacity()` — the principal with no accrued interest.

This is the exact structural analog to the `vePeg` report: a comparison `x > threshold` that silently fails when `x` equals a legitimate sentinel value (`0`) representing a distinct, valid state.

---

### Impact Explanation

**Tx-pool rejection (primary impact).** `check_tx_fee` in `tx-pool/src/util.rs` calls `DaoCalculator::transaction_fee`, which calls `transaction_maximum_withdraw`:

```rust
let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
    .transaction_fee(rtx)
    .map_err(|err| {
        Reject::Malformed(format!("{err}"), "expect (outputs capacity) <= (inputs capacity)".to_owned())
    })?;
``` [2](#0-1) 

`transaction_fee` is `maximum_withdraw - outputs_capacity`. For a genesis-block withdrawal, `maximum_withdraw` is the raw principal (no interest), while `outputs_capacity` legitimately includes accrued interest. The subtraction underflows, `safe_sub` returns `Err(Overflow)`, and the transaction is rejected with `Reject::Malformed`. The user can never submit the withdrawal through the normal tx-pool path.

**Block-verification fee miscalculation (secondary impact).** `FeeCalculator::transaction_fee` in `verification/src/transaction_verifier.rs` uses the same `DaoCalculator::transaction_fee`:

```rust
fn transaction_fee(&self) -> Result<Capacity, DaoError> {
    if self.transaction.is_cellbase() {
        Ok(Capacity::zero())
    } else {
        DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
            .transaction_fee(&self.transaction)
    }
}
``` [3](#0-2) 

This is called inside `ContextualTransactionVerifier::complete` at line 208, which feeds the fee into the `Completed` result used by the reward calculator. A wrong fee value propagates into block reward accounting.

**Impact: Medium** — a user with a genesis-block DAO deposit is permanently unable to claim interest via the standard tx-pool path; the withdrawal is rejected as malformed before it can be included in a block.

---

### Likelihood Explanation

**Likelihood: Medium.** The CKB chain spec allows `issued_cells` with arbitrary type scripts, including the DAO type script. Any chain operator (mainnet, testnet, or custom devnet) can configure genesis cells that are valid DAO deposits. The vulnerability is triggered by any ordinary transaction sender submitting a phase-2 DAO withdrawal for such a cell — no special privilege is required. The attacker-controlled entry path is the standard `send_transaction` RPC call.

---

### Recommendation

Replace the sentinel-value check with an explicit comparison against the deposit header's block number, or use a dedicated flag to distinguish deposit cells from withdrawal cells. The minimal fix mirrors the `vePeg` recommendation — extend the condition to also accept `deposited_block_number == 0` when the cell data is exactly 8 zero bytes and the provided deposit header is the genesis block:

```diff
- if deposited_block_number > 0 {
+ if deposited_block_number > 0 || (deposited_block_number == 0 && cell_data_is_withdrawal_marker) {
```

A cleaner approach is to verify the deposit header number against `deposited_block_number` unconditionally (including when it is zero), and treat the cell as a deposit cell only when the data is absent or malformed:

```diff
- if deposited_block_number > 0 {
+ let is_withdrawal_cell = match self.data_loader.load_cell_data(cell_meta) {
+     Some(data) if data.len() == 8 => {
+         // A withdrawal cell stores the deposit block number; a deposit cell stores all zeros.
+         // Both encode to 0 when the deposit is in block 0, so we must also verify
+         // the deposit header exists and matches.
+         let n = LittleEndian::read_u64(&data);
+         // treat as withdrawal if witness + header_dep resolves to a header whose number == n
+         ...
+     }
+     _ => false,
+ };
+ if is_withdrawal_cell {
```

The root fix is to not rely on `deposited_block_number > 0` as the sole discriminator between deposit and withdrawal cells.

---

### Proof of Concept

1. Configure a CKB chain with a genesis issued cell that has the DAO type script and 8 bytes of zero data (a valid DAO deposit cell in block 0).
2. Mine enough blocks to accumulate interest.
3. Submit a phase-1 withdrawal transaction (creates a withdrawal cell with data = `0u64.to_le_bytes()` = `[0,0,0,0,0,0,0,0]`).
4. Submit the phase-2 withdrawal transaction via `send_transaction` RPC. The outputs include the principal plus accrued interest.
5. Observe: `check_tx_fee` calls `transaction_maximum_withdraw`, reads `deposited_block_number = 0`, falls into the `else` branch at line 114–116, returns only the raw principal. `transaction_fee = principal - (principal + interest)` underflows, `safe_sub` returns `Err(Overflow)`, and the RPC returns `Reject::Malformed("...", "expect (outputs capacity) <= (inputs capacity)")`.

The transaction is permanently blocked from the tx-pool despite being a fully valid DAO withdrawal. [4](#0-3) [5](#0-4) [3](#0-2)

### Citations

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

**File:** verification/src/transaction_verifier.rs (L265-273)
```rust
    fn transaction_fee(&self) -> Result<Capacity, DaoError> {
        // skip tx fee calculation for cellbase
        if self.transaction.is_cellbase() {
            Ok(Capacity::zero())
        } else {
            DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
                .transaction_fee(&self.transaction)
        }
    }
```
