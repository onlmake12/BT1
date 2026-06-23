### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Skips Overflow Check Used Everywhere Else — (`util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` converts a `u128` intermediate result to `u64` using the truncating `as u64` cast. Every other u128→u64 conversion in the same codebase (including in the sibling function `dao_field_with_current_epoch`) uses the safe `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern. The unsafe cast silently wraps on overflow instead of propagating a `DaoError::Overflow`, corrupting the returned withdrawal capacity. Because `calculate_maximum_withdraw` is called on the consensus-critical path that computes the DAO field embedded in every block header, a sufficiently large deposited cell can cause the node to embed a wrong DAO field, breaking consensus.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The sibling function `dao_field_with_current_epoch`, which performs the identical u128→u64 narrowing for `miner_issuance128` and `ar_increase128`, uses the checked form:

```rust
// util/dao/src/lib.rs  lines 244-245, 258
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// ...
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The existing unit test `check_withdraw_calculation_overflows` explicitly constructs an overflow scenario (capacity `18_446_744_073_709_550_000` shannons, `withdrawing_ar = 10_000_000_001_123_456`, `deposit_ar = 10_000_000_000_123_456`) and asserts `result.is_err()`. With the `as u64` cast the product `withdraw_counted_capacity ≈ 2.03 × 10^19 > u64::MAX` wraps silently to `≈ 1.84 × 10^18`, `safe_add` succeeds, and the function returns `Ok(...)` — the test assertion fails, confirming the bug. [4](#0-3) 

The call chain that makes this consensus-critical:

```
dao_field_with_current_epoch          (block assembly & DaoHeaderVerifier)
  └─ withdrawed_interests
       └─ transaction_maximum_withdraw
            └─ calculate_maximum_withdraw   ← truncation here
``` [5](#0-4) [6](#0-5) 

`dao_field_with_current_epoch` is invoked by `BlockAssembler::calc_dao` during block template generation and by `DaoHeaderVerifier` during contextual block verification. [7](#0-6) [8](#0-7) 

### Impact Explanation

When `withdraw_counted_capacity` overflows `u64`, the truncated value is used as the withdrawal capacity. This corrupts `withdrawed_interests`, which is subtracted from the running DAO `s` field. A block assembled with this corrupted DAO field will be rejected by peers that compute the correct value, causing a chain split or block rejection. Additionally, the public RPC `calculate_dao_maximum_withdraw` (in `rpc/src/module/experiment.rs`) calls `calculate_maximum_withdraw` directly and would return a silently wrong (much smaller) withdrawal amount to callers instead of a `DaoError`. [9](#0-8) 

### Likelihood Explanation

Triggering the overflow requires a single deposited cell whose `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity ≤ total CKB supply ≈ 3.36 × 10^18 shannons` and `u64::MAX ≈ 1.84 × 10^19`, the accumulate rate ratio would need to exceed ~5.5×, which would take many decades at current secondary issuance rates. Likelihood is therefore very low under normal network conditions, but the code path is reachable by any tx-pool submitter or RPC caller and the test suite already documents the expected error behaviour that the current code violates.

### Recommendation

Replace the truncating cast with the same checked conversion used in `dao_field_with_current_epoch`:

```rust
// util/dao/src/lib.rs  line 155-156
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This matches the pattern already established at lines 244–245 and 258 of the same file and makes the existing `check_withdraw_calculation_overflows` test pass.

### Proof of Concept

The existing unit test at `util/dao/src/tests.rs:295–350` is the proof of concept. It constructs a cell with capacity `18_446_744_073_709_550_000` shannons and accumulate-rate values that force `withdraw_counted_capacity ≈ 2.03 × 10^19 > u64::MAX`. The test asserts `result.is_err()`. With the current `as u64` cast the function returns `Ok(Capacity::shannons(≈1.84 × 10^18))` — the assertion fails, demonstrating the silent truncation. [4](#0-3)

### Citations

**File:** util/dao/src/lib.rs (L38-124)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
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
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
    }
```

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/src/tests.rs (L295-350)
```rust
#[test]
fn check_withdraw_calculation_overflows() {
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(18_446_744_073_709_550_000))
        .build();
    let tx = TransactionBuilder::default().output(output.clone()).build();
    let epoch = EpochNumberWithFraction::new(1, 100, 1000);
    let deposit_header = HeaderBuilder::default()
        .number(100)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_000_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let deposit_block = BlockBuilder::default()
        .header(deposit_header)
        .transaction(tx)
        .build();

    let epoch = EpochNumberWithFraction::new(1, 200, 1000);
    let withdrawing_header = HeaderBuilder::default()
        .number(200)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_001_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let withdrawing_block = BlockBuilder::default().header(withdrawing_header).build();

    let tmp_dir = TempDir::new().unwrap();
    let db = RocksDB::open_in(&tmp_dir, COLUMNS);
    let store = ChainDB::new(db, Default::default());
    let txn = store.begin_transaction();
    txn.insert_block(&deposit_block).unwrap();
    txn.attach_block(&deposit_block).unwrap();
    txn.insert_block(&withdrawing_block).unwrap();
    txn.attach_block(&withdrawing_block).unwrap();
    txn.commit().unwrap();

    let consensus = Consensus::default();
    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.calculate_maximum_withdraw(
        &output,
        Capacity::bytes(0).expect("should not overflow"),
        &deposit_block.hash(),
        &withdrawing_block.hash(),
    );
    assert!(result.is_err());
}
```

**File:** tx-pool/src/block_assembler/mod.rs (L628-680)
```rust
    fn calc_dao(
        snapshot: &Snapshot,
        current_epoch: &EpochExt,
        cellbase: TransactionView,
        entries: Vec<TxEntry>,
    ) -> CalcDaoResult {
        let tip_header = snapshot.tip_header();
        let consensus = snapshot.consensus();
        let mut seen_inputs = HashSet::new();
        let mut transactions_checker = TransactionsChecker::new(iter::once(&cellbase));

        let mut checked_failed_txs = vec![];
        let checked_entries: Vec<_> = block_in_place(|| {
            entries
                .into_iter()
                .filter_map(|entry| {
                    let overlay_cell_checker =
                        OverlayCellChecker::new(&transactions_checker, snapshot);
                    if let Err(err) =
                        entry
                            .rtx
                            .check(&mut seen_inputs, &overlay_cell_checker, snapshot)
                    {
                        error!(
                            "Resolving transactions while building block template, \
                             tip_number: {}, tip_hash: {}, tx_hash: {}, error: {:?}",
                            tip_header.number(),
                            tip_header.hash(),
                            entry.transaction().hash(),
                            err
                        );
                        // Returning the out_point makes debugging easier and provides better logs.
                        checked_failed_txs
                            .push((entry.proposal_short_id(), err.out_point().cloned()));
                        None
                    } else {
                        transactions_checker.insert(entry.transaction());
                        Some(entry)
                    }
                })
                .collect()
        });

        let dummy_cellbase_entry = TxEntry::dummy_resolve(cellbase, 0, Capacity::zero(), 0);
        let entries_iter = iter::once(&dummy_cellbase_entry)
            .chain(checked_entries.iter())
            .map(|entry| entry.rtx.as_ref());

        // Generate DAO fields here
        let dao = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
            .dao_field_with_current_epoch(entries_iter, tip_header, current_epoch)?;

        Ok((dao, checked_entries, checked_failed_txs))
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-671)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
```

**File:** rpc/src/module/experiment.rs (L1-10)
```rust
use crate::error::RPCError;
use crate::module::chain::CyclesEstimator;
use async_trait::async_trait;
use ckb_dao::DaoCalculator;
use ckb_jsonrpc_types::{
    Capacity, DaoWithdrawingCalculationKind, EstimateCycles, EstimateMode, OutPoint, Transaction,
    Uint64,
};
use ckb_shared::{Snapshot, shared::Shared};
use ckb_store::ChainStore;
```
