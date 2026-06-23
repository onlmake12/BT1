### Title
DAO Withdrawal `header_dep_index` Interpretation Mismatch Between Rust `DaoCalculator` and C VM DAO Script Causes Consensus Split — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the full u64 `header_dep_index` from the witness to locate the deposit block header, while the C VM DAO script reads only the **lowest byte** (u8) of the same field. This discrepancy means the two implementations can resolve different headers for the same witness value, creating a consensus split: blocks that the C VM considers valid are rejected by the Rust node's `DaoHeaderVerifier`.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` reads the `header_dep_index` as a full u64 and uses it to index into `header_deps`: [1](#0-0) 

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// ...
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // full u64 used as index
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
```

The C VM DAO script, however, reads only the **lowest byte** of this u64. This discrepancy is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`: [2](#0-1) 

The test comment reads:
> "Position 1: correct deposit block (what C VM resolves via lowest byte). Position 257: withdraw block (wrong — Rust resolves this with full u64)."

The `DaoCalculator` is not merely used for fee estimation — it is the authoritative source for the `dao_field` value embedded in every block header. `DaoHeaderVerifier::verify` calls `DaoCalculator::dao_field`, which internally calls `withdrawed_interests` → `transaction_maximum_withdraw`. If `transaction_maximum_withdraw` returns an error (because the full-u64 index resolves to a wrong header), `DaoHeaderVerifier` returns `BlockErrorKind::InvalidDAO` and the entire block is rejected. [3](#0-2) 

The call chain is:

```
DaoHeaderVerifier::verify
  └─ DaoCalculator::dao_field
       └─ dao_field_with_current_epoch
            └─ withdrawed_interests
                 └─ transaction_maximum_withdraw   ← uses full u64 index
``` [4](#0-3) 

---

### Impact Explanation

An attacker (or any miner using a non-Rust implementation that faithfully follows the C VM DAO script) can craft a DAO withdrawal transaction where:

- `header_dep_index` in the witness = `257` (u64 little-endian)
- `header_deps[1]` (lowest byte = 1) = the **correct** deposit block (block number matches cell data)
- `header_deps[257]` (full u64 = 257) = a **different** block (block number does not match)

**C VM DAO script**: reads byte `1` → resolves `header_deps[1]` = correct deposit block → **accepts** the transaction.

**Rust `DaoCalculator`**: reads u64 `257` → resolves `header_deps[257]` = wrong block → block number check at line 105 fails → returns `DaoError::InvalidOutPoint` → `DaoHeaderVerifier` returns `BlockErrorKind::InvalidDAO` → **rejects the block**.

A miner using a non-Rust implementation includes such a transaction in a block. The block is valid per the C VM but rejected by every Rust node. This causes a **consensus split / chain fork**.

---

### Likelihood Explanation

- Requires a DAO withdrawal transaction with `header_dep_index > 255`, meaning `header_deps` must contain more than 255 entries. This is unusual but not prohibited by the protocol.
- Requires a miner using a non-Rust implementation (or a modified Rust node) that follows the C VM's lowest-byte interpretation.
- The discrepancy is already documented in a test, indicating the developers are aware of the behavioral difference but have not aligned the two implementations.
- Likelihood is **low** in practice today, but the attack surface grows if alternative CKB node implementations emerge.

---

### Recommendation

Align the Rust `DaoCalculator` with the C VM DAO script by reading only the lowest byte of `header_dep_index`:

```rust
// Current (full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed (lowest byte only, matching C VM behavior):
Ok(header_deps_index_data.unwrap()[0] as u64)
```

Alternatively, update the C VM DAO script to use the full u64 value and document this as a consensus rule change requiring a hard fork. Either way, the two implementations must agree on the same interpretation. [5](#0-4) 

---

### Proof of Concept

The existing test in `util/dao/src/tests.rs` already demonstrates the discrepancy: [6](#0-5) 

To trigger the consensus split:

1. Construct a DAO withdrawal transaction with 258 `header_deps`.
2. Place the correct deposit block hash at index `1` and a dummy block hash at index `257`.
3. Set the witness `input_type` to `257u64` (little-endian 8 bytes).
4. Submit this transaction to a non-Rust miner that follows the C VM's lowest-byte interpretation.
5. The miner includes it in a block; the block passes C VM script validation.
6. Rust nodes call `DaoHeaderVerifier::verify` → `DaoCalculator::dao_field` → `transaction_maximum_withdraw` → resolves `header_deps[257]` (wrong block) → block number mismatch → `BlockErrorKind::InvalidDAO` → block rejected.
7. Chain split between Rust nodes and non-Rust nodes.

### Citations

**File:** util/dao/src/lib.rs (L79-99)
```rust
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
```

**File:** util/dao/src/lib.rs (L208-264)
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
    }
```

**File:** util/dao/src/tests.rs (L475-536)
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
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
```
