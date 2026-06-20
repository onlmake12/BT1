### Title
NervosDAO Phase-2 Withdrawal Permanently Blocked for Genesis-Block (Block 0) Deposits — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::transaction_maximum_withdraw` uses `deposited_block_number > 0` to distinguish a deposit cell (all-zero 8-byte data) from a prepare cell (8-byte deposit block number). When a DAO deposit is made in block 0, the prepare cell's data encodes `0u64` — byte-for-byte identical to a deposit cell. The `> 0` guard silently misclassifies the prepare cell as a deposit cell, returns face value instead of face value + interest, and causes the fee calculation to underflow. The tx pool rejects the Phase-2 withdrawal as `Malformed`, permanently locking the user's funds in the prepare cell.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the 8-byte cell data of every DAO-type input and interprets it as the deposit block number:

```rust
let deposited_block_number =
    match self.data_loader.load_cell_data(cell_meta) {
        Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
        _ => 0,
    };
if deposited_block_number > 0 {          // ← the guard
    // full interest calculation (Phase-2 path)
} else {
    Ok(output.capacity().into())         // ← deposit-cell path: face value only
}
``` [1](#0-0) 

The NervosDAO protocol encodes the two cell states as:

| Cell state | 8-byte data |
|---|---|
| Deposit cell | `[0,0,0,0,0,0,0,0]` (all zeros) |
| Prepare cell (deposited at block N) | `N` as little-endian u64 |

When `N = 0` (genesis block), the prepare cell data is `[0,0,0,0,0,0,0,0]` — indistinguishable from a deposit cell. The `> 0` guard therefore routes a valid Phase-2 input through the deposit-cell branch, returning only face value as the maximum withdraw.

`transaction_fee` then computes:

```rust
maximum_withdraw.safe_sub(outputs_capacity)
``` [2](#0-1) 

A correct Phase-2 transaction claims `face_value + interest` in its output. With `maximum_withdraw = face_value`, the subtraction underflows and returns `DaoError::Overflow`.

This error propagates through two independent rejection paths:

**Path 1 — tx-pool pre-check (`check_tx_fee`):**

```rust
let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
    .transaction_fee(rtx)
    .map_err(|err| {
        Reject::Malformed(format!("{err}"), "expect (outputs capacity) <= (inputs capacity)".to_owned())
    })?;
``` [3](#0-2) 

**Path 2 — contextual block verification (`FeeCalculator::transaction_fee`):**

```rust
DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
    .transaction_fee(&self.transaction)
``` [4](#0-3) 

Both paths reject the transaction before the on-chain DAO C script ever runs. The user cannot complete Phase-2 withdrawal through any normal submission route.

### Impact Explanation

A user who deposited CKB into NervosDAO in block 0 and successfully completed Phase-1 (prepare) is permanently unable to complete Phase-2 withdrawal. Their CKB capacity (principal + accrued interest) is locked in the prepare cell indefinitely. No alternative code path exists: the same `DaoCalculator` is invoked by both the tx pool and the block verifier, so even a miner assembling a block directly would produce an invalid DAO field (because `withdrawed_interests` also calls `transaction_maximum_withdraw` and would return 0 interest, corrupting the `S` accumulator in the DAO field). [5](#0-4) 

### Likelihood Explanation

On the live CKB mainnet the genesis block is fixed and contains no user DAO deposits, so the condition is not currently triggered there. However:

- The CKB protocol places no consensus-level restriction preventing a DAO deposit in block 0.
- Any operator running a custom chain (dev, testnet, or a fork) who includes a DAO deposit in the genesis block will reproduce this bug exactly.
- The `spec/src/lib.rs` genesis builder already supports arbitrary transactions in block 0, and the `spec/src/consensus.rs` default builder constructs a genesis block at `EpochNumberWithFraction::new_unchecked(0, 0, 0)` with block number 0. [6](#0-5) [7](#0-6) 

The bug is latent in all deployed CKB node versions and is a single-transaction-sender–reachable issue on any chain where block 0 holds a DAO deposit.

### Recommendation

Replace the `> 0` sentinel with an explicit check on whether the 8-byte cell data is all zeros (the canonical deposit-cell marker), independent of the numeric value of the block number:

```rust
let is_prepare_cell = match self.data_loader.load_cell_data(cell_meta) {
    Some(data) if data.len() == 8 => data.iter().any(|b| *b != 0),
    _ => false,
};
if is_prepare_cell {
    let deposited_block_number = LittleEndian::read_u64(&...);
    // full interest calculation
} else {
    Ok(output.capacity().into())
}
```

This correctly handles a deposit at block 0 because the prepare cell for block 0 would still have all-zero data, but the deposit-cell path is the right path for it — the real fix is to ensure Phase-1 (prepare) for a block-0 deposit writes a non-zero sentinel (e.g., a flag byte) or that the protocol explicitly forbids block-0 DAO deposits via a consensus rule.

### Proof of Concept

1. Construct a chain where the genesis block (number 0) contains a DAO deposit transaction.
2. Mine enough blocks for the deposit to mature.
3. Submit Phase-1 (prepare): the prepare cell's 8-byte data is written as `0u64` = `[0,0,0,0,0,0,0,0]`.
4. Attempt Phase-2 (withdraw) claiming `face_value + interest`:
   - `tx-pool/src/util.rs::check_tx_fee` calls `DaoCalculator::transaction_fee`.
   - `transaction_maximum_withdraw` reads `deposited_block_number = 0`.
   - `0 > 0` is false → returns `face_value` as maximum withdraw.
   - `face_value.safe_sub(face_value + interest)` underflows → `DaoError::Overflow`.
   - `check_tx_fee` maps this to `Reject::Malformed(...)`.
5. The transaction is rejected. The user's CKB is permanently locked in the prepare cell.

The existing test `check_dao_withdraw_block_number_match` (deposit at block 100) passes, but an equivalent test with `deposit_number = 0` would demonstrate the failure:

```rust
let rtx = build_dao_withdraw_tx(&deposit_block, &withdraw_block, 0u64);
// DaoCalculator treats deposited_block_number == 0 as a deposit cell
// transaction_fee returns Overflow instead of Ok(interest)
assert!(calculator.transaction_fee(&rtx).is_err()); // wrong path taken
``` [8](#0-7) [9](#0-8)

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

**File:** util/dao/src/lib.rs (L61-116)
```rust
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

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
    }
```

**File:** tx-pool/src/util.rs (L34-41)
```rust
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
```

**File:** verification/src/transaction_verifier.rs (L270-272)
```rust
            DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
                .transaction_fee(&self.transaction)
        }
```

**File:** spec/src/lib.rs (L640-651)
```rust
        let block = BlockBuilder::default()
            .version(self.genesis.version)
            .parent_hash(&self.genesis.parent_hash)
            .timestamp(self.genesis.timestamp)
            .compact_target(self.genesis.compact_target)
            .extra_hash(&self.genesis.uncles_hash)
            .epoch(EpochNumberWithFraction::new_unchecked(0, 0, 0))
            .dao(dao)
            .nonce(u128::from_le_bytes(self.genesis.nonce.to_le_bytes()))
            .transaction(cellbase_transaction)
            .transaction(dep_group_transaction)
            .build();
```

**File:** spec/src/consensus.rs (L202-207)
```rust
        let genesis_block = BlockBuilder::default()
            .compact_target(DIFF_TWO)
            .epoch(EpochNumberWithFraction::new_unchecked(0, 0, 0))
            .dao(dao)
            .transaction(cellbase)
            .build();
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
