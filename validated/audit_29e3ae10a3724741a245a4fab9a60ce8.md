### Title
DAO Withdrawal `header_deps_index` Interpretation Mismatch Between On-Chain C Script and Rust `DaoCalculator` Causes Valid Withdrawals to Be Rejected and DAO Field Miscalculation — (File: `util/dao/src/lib.rs`)

---

### Summary

The on-chain NervosDAO C script (`dao.c`) reads only the **lowest byte (u8)** of the 8-byte little-endian `header_deps_index` stored in the `WitnessArgs.input_type` field, while the Rust `DaoCalculator::transaction_maximum_withdraw` reads the **full u64**. For any `header_deps_index ≥ 256`, the two components resolve different deposit block headers. This causes: (1) valid DAO phase-2 withdrawals to be rejected by the tx-pool, and (2) incorrect `withdrawed_interests` in `dao_field_with_current_epoch`, producing a wrong DAO field in assembled blocks that other nodes will reject.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, `transaction_maximum_withdraw`:**

The Rust code reads the full 8-byte little-endian u64 from the witness:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and uses it directly as a `usize` index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [1](#0-0) 

The on-chain DAO C script, however, reads only the **lowest byte** of the same 8-byte field when resolving the deposit header index. This is explicitly documented in the production test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [2](#0-1) 

The test asserts `result.is_err()` — Rust rejects the transaction — while the comment states the C VM would accept it (using index 1 = deposit block). This is the exact same class of mismatch as the external report: two components of the same system interpret the same stored value using different units/widths, producing divergent accounting results.

**Secondary propagation — `dao_field_with_current_epoch`:**

`dao_field_with_current_epoch` calls `withdrawed_interests`, which calls `transaction_maximum_withdraw` with the same buggy index resolution. If a DAO withdrawal with `header_deps_index ≥ 256` is included in a block, the `current_s` (secondary issuance tracking) field in the DAO header field is computed from the wrong deposit AR, producing an incorrect `Byte32` DAO field. [3](#0-2) 

The `withdrawed_interests` subtraction at line 254 uses the wrong maximum-withdraw value, corrupting `current_s`: [4](#0-3) 

---

### Impact Explanation

**Impact 1 — Valid DAO withdrawal censored at tx-pool level (DoS):**
A transaction sender submits a phase-2 DAO withdrawal with `header_deps_index = 257` (lowest byte = 1). The on-chain C script resolves index 1 (deposit block) and accepts the transaction. Rust resolves index 257 (a different block), the block-number cross-check at line 105 fails, and the tx-pool rejects the transaction with `DaoError::InvalidOutPoint`. The user's valid withdrawal is permanently blocked from entering the tx-pool. [5](#0-4) 

**Impact 2 — Incorrect DAO field in assembled block (consensus split):**
If a miner directly includes such a transaction in a block (bypassing the tx-pool), `dao_field_with_current_epoch` computes `withdrawed_interests` using the wrong deposit header's AR value. The resulting DAO field in the block header is incorrect. Peer nodes recompute the DAO field independently and find a mismatch, causing them to reject the block. This creates a consensus-level block rejection for any block containing such a transaction. [6](#0-5) 

---

### Likelihood Explanation

**Medium-low.** Exploiting Impact 1 requires a transaction with ≥ 257 `header_deps` entries, which is unusual but not blocked by any consensus rule (the transaction size limit is the only constraint). A motivated attacker who has a DAO deposit can craft such a transaction. Impact 2 requires a miner to directly include the transaction, which is self-defeating (their block is rejected), but could be used to waste network resources or test the discrepancy. The discrepancy is already documented in the production test suite, indicating the CKB developers are aware of the C script's behavior.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain C script's actual byte-width when reading `header_deps_index`. If the C script reads only 1 byte, Rust should do the same:

```rust
// Read only the lowest byte, matching dao.c behavior
let index = header_deps_index_data.unwrap()[0] as usize;
```

Alternatively, fix the on-chain C script to read the full u64 (matching Rust), and deploy the fix via a hardfork or script upgrade. Either way, both components must agree on the same interpretation of the 8-byte witness field.

---

### Proof of Concept

The discrepancy is directly demonstrated by the existing production test in `util/dao/src/tests.rs`:

1. Build a DAO withdrawal transaction with 258 `header_deps`.
2. Place the deposit block hash at index 1 and the withdraw block hash at index 257.
3. Set `WitnessArgs.input_type` = `257u64.to_le_bytes()` (lowest byte = 1).
4. Call `DaoCalculator::transaction_fee(&rtx)`.

**C VM behavior**: reads lowest byte → index 1 → deposit block (number 100) → block-number check passes → accepts.

**Rust behavior**: reads full u64 → index 257 → withdraw block (number 200) → block-number check `200 != 100` → `DaoError::InvalidOutPoint` → rejects.

The test asserts `result.is_err()`, confirming Rust rejects what the C VM accepts. [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L91-96)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
```

**File:** util/dao/src/lib.rs (L101-107)
```rust
                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
```

**File:** util/dao/src/lib.rs (L208-263)
```rust
    /// Calculates the new dao field with specified [`EpochExt`].
    pub fn dao_field_with_current_epoch(
        &self,
        rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
        parent: &HeaderView,
        current_block_epoch: &EpochExt,
    ) -> Result<Byte32, DaoError> {
        // Freed occupied capacities from consumed inputs
        let freed_occupied_capacities =
            rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
                self.input_occupied_capacities(rtx)
                    .and_then(|c| capacities.safe_add(c))
            })?;
        let added_occupied_capacities = self.added_occupied_capacities(rtxs.clone())?;
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;

        let (parent_ar, parent_c, parent_s, parent_u) = extract_dao_data(parent.dao());

        // g contains both primary issuance and secondary issuance,
        // g2 is the secondary issuance for the block, which consists of
        // issuance for the miner, NervosDAO and treasury.
        // When calculating issuance in NervosDAO, we use the real
        // issuance for each block(which will only be issued on chain
        // after the finalization delay), not the capacities generated
        // in the cellbase of current block.
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;

        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;

        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
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

**File:** util/dao/src/tests.rs (L475-537)
```rust
#[test]
fn check_dao_withdraw_header_dep_index_exceeds_u8() {
    let deposit_number = 100u64;
    let withdraw_number = 200u64;

    let (_tmp_dir, store, deposit_block, withdraw_block) =
        setup_store_with_headers(deposit_number, withdraw_number);

    let consensus = Consensus::default();
    let dao_type_script = Script::new_builder()
        .code_hash(consensus.dao_type_hash())
        .hash_type(ScriptHashType::Type)
        .build();

    // Pad header_deps to 258 entries so index 257 is valid.
    // Position 1: correct deposit block (what C VM resolves via lowest byte).
    // Position 257: withdraw block (wrong — Rust resolves this with full u64).
    let dummy = h256!("0x1").into();
    let mut header_deps = vec![dummy; 258];
    header_deps[1] = deposit_block.hash();
    header_deps[257] = withdraw_block.hash();

    let cell_data = Bytes::from(deposit_number.to_le_bytes().to_vec());
    let input_cell = CellOutput::new_builder()
        .capacity(capacity_bytes!(1000000))
        .type_(Some(dao_type_script).pack())
        .build();
    let tx_info = TransactionInfo::new(
        withdraw_block.number(),
        withdraw_block.epoch(),
        withdraw_block.hash(),
        0,
    );
    let cell_meta = CellMetaBuilder::from_cell_output(input_cell, cell_data)
        .transaction_info(tx_info)
        .build();

    // input_type = 257, lowest byte = 1
    let witness = WitnessArgs::new_builder()
        .input_type(Some(Bytes::from(257u64.to_le_bytes().to_vec())))
        .build();
    let witness_bytes: Bytes = witness.as_bytes();

    let tx = TransactionBuilder::default()
        .set_header_deps(header_deps)
        .witness(witness_bytes)
        .build();

    let rtx = ResolvedTransaction {
        transaction: tx,
        resolved_cell_deps: vec![],
        resolved_inputs: vec![cell_meta],
        resolved_dep_groups: vec![],
    };

    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.transaction_fee(&rtx);

    // Rust resolves index 257 → withdraw block (number 200), but cell data
    // says deposited at block 100. Block number check catches the mismatch.
    assert!(result.is_err(), "expected Err, got {result:?}");
}
```
