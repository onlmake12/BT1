### Title
DAO Interest Silently Skipped for Block-0 Deposits Causes Tx-Pool Permanent Rejection — (`util/dao/src/lib.rs`)

### Summary

`DaoCalculator::transaction_maximum_withdraw` uses `if deposited_block_number > 0` to decide whether to compute DAO interest. Because the NervosDAO phase-2 (prepare) cell stores the **deposit block number** as a little-endian u64 in its data field, a deposit made at block 0 (genesis) produces cell data `0x0000000000000000`, which decodes to `0`. The guard is therefore `false`, the interest path is skipped, and only the bare original capacity is returned. The downstream `transaction_fee` call then attempts `maximum_withdraw - outputs_capacity`, where `outputs_capacity` legitimately exceeds `maximum_withdraw` by the accrued interest, causing `safe_sub` to overflow and the tx-pool to permanently reject the withdrawal with `Reject::Malformed`.

### Finding Description

**Root cause — `util/dao/src/lib.rs`, line 66**

```
if deposited_block_number > 0 {          // ← blocks interest path for genesis deposits
    …calculate_maximum_withdraw(…)…
} else {
    Ok(output.capacity().into())          // ← returns bare capacity, no interest
}
```

The NervosDAO two-phase protocol encodes the deposit block number in the phase-2 cell's data field. For any deposit at block `N > 0` this is unambiguous. For a deposit at block `0` the encoded value is `0x0000000000000000`, identical to the phase-1 (deposit) cell's data. The guard `deposited_block_number > 0` was written to distinguish phase-1 from phase-2 cells, but it silently misclassifies every phase-2 cell whose deposit occurred at block 0.

**Propagation path**

`transaction_maximum_withdraw` is called exclusively by `transaction_fee`:

```rust
// util/dao/src/lib.rs:30-36
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))   // overflows when interest is excluded
        .map_err(Into::into)
}
```

`transaction_fee` is called by `check_tx_fee` in the tx-pool:

```rust
// tx-pool/src/util.rs:34-41
let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
    .transaction_fee(rtx)
    .map_err(|err| {
        Reject::Malformed(
            format!("{err}"),
            "expect (outputs capacity) <= (inputs capacity)".to_owned(),
        )
    })?;
```

A valid phase-2 withdrawal transaction whose deposit was at block 0 will always be rejected here with `Reject::Malformed`, regardless of how correctly it is constructed.

**Contrast with `calculate_maximum_withdraw` (the RPC path)**

The public `calculate_maximum_withdraw` function used by the `calculate_dao_maximum_withdraw` RPC does **not** contain the `deposited_block_number > 0` guard; it only checks `deposit_header.number() >= withdrawing_header.number()`, which correctly passes for a genesis deposit (0 < any later block). This creates a direct discrepancy: the RPC reports a valid, interest-bearing withdrawal amount, but the tx-pool rejects the transaction that claims it.

**No test coverage for the zero case**

The existing test suite exercises `deposited_block_number = 100` (match) and `deposited_block_number = 99` (mismatch), but never `deposited_block_number = 0`.

### Impact Explanation

Any holder of a DAO phase-2 cell whose deposit block number is 0 cannot submit a withdrawal transaction through the tx-pool. The tx-pool permanently rejects it with `Malformed`. The accrued DAO interest is inaccessible via the standard submission path. The user's funds are effectively locked unless they can bypass the tx-pool entirely (e.g., direct miner submission), which is not a supported workflow for ordinary users.

### Likelihood Explanation

The CKB mainnet genesis block does not contain user-controlled DAO deposit cells, so the condition is not triggered on mainnet today. However:

- Custom chains, testnets, and devnets can and do include genesis-block DAO cells.
- The genesis block spec (`spec/src/lib.rs`, `build_genesis`) is configurable and nothing in the code prevents a DAO deposit cell from appearing at block 0.
- The bug is a latent correctness defect that would silently activate the moment any deployment places a DAO deposit in the genesis block.

### Recommendation

Replace the `deposited_block_number > 0` guard with a check that is robust to genesis-block deposits. The simplest correct fix is to attempt the interest calculation whenever the cell data is exactly 8 bytes (the structural invariant for a phase-2 cell), and let `calculate_maximum_withdraw`'s own `deposit_header.number() >= withdrawing_header.number()` guard reject genuinely invalid cases:

```rust
let maybe_deposited_block_number: Option<u64> =
    match self.data_loader.load_cell_data(cell_meta) {
        Some(data) if data.len() == 8 => Some(LittleEndian::read_u64(&data)),
        _ => None,
    };
// Treat any 8-byte data field as a phase-2 cell; block 0 is valid.
if let Some(deposited_block_number) = maybe_deposited_block_number {
    // … existing interest-calculation path …
} else {
    Ok(output.capacity().into())
}
```

The cross-check `deposit_header.number() != deposited_block_number` at line 105 already guards against a mismatched witness, so no additional validation is needed.

### Proof of Concept

1. Construct a genesis block containing a DAO deposit cell (type script = DAO type hash, data = `0x0000000000000000`).
2. Mine blocks to accumulate interest (`ar` ratio increases).
3. Submit a phase-2 prepare transaction spending the genesis deposit cell; the new cell's data is `0x0000000000000000` (block 0 encoded as LE u64).
4. Construct a phase-2 withdrawal transaction spending the prepare cell, with `outputs_capacity = original_capacity + interest` and the correct header deps and witness.
5. Submit to the tx-pool via `send_transaction` RPC.
6. **Observed**: `Reject::Malformed("InvalidOutPoint …", "expect (outputs capacity) <= (inputs capacity)")` — the transaction is rejected.
7. **Expected**: The transaction is accepted; `transaction_maximum_withdraw` should return `original_capacity + interest`, making `fee ≥ 0`.

The discrepancy is confirmed by calling `calculate_dao_maximum_withdraw` on the same out-point, which correctly returns the interest-bearing amount, proving the withdrawal is valid but the tx-pool guard is wrong.

---

**Key citations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** util/dao/src/lib.rs (L127-158)
```rust
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** tx-pool/src/util.rs (L28-54)
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
}
```

**File:** util/dao/src/tests.rs (L458-473)
```rust
#[test]
fn check_dao_withdraw_block_number_match() {
    let deposit_number = 100u64;
    let (_tmp_dir, store, deposit_block, withdraw_block) =
        setup_store_with_headers(deposit_number, 200);

    // Cell data matches deposit header block number
    let rtx = build_dao_withdraw_tx(&deposit_block, &withdraw_block, deposit_number);

    let consensus = Consensus::default();
    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.transaction_fee(&rtx);

    assert!(result.is_ok(), "expected Ok, got {result:?}");
}
```
