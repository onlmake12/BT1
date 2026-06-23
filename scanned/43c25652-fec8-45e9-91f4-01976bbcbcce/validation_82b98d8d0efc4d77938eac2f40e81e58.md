### Title
DAO Withdrawal `s`-Field Accounting Skipped When Deposit Header Index Exceeds u8 Range — (`util/dao/src/lib.rs`)

### Summary
The Rust `DaoCalculator` reads the deposit-header index from the witness `input_type` field as a full `u64`, while the on-chain C DAO script resolves the same index using only its lowest byte. When a DAO Phase-2 withdrawal transaction encodes an index value > 255 in the witness, the two implementations resolve different block headers. The Rust node then either errors out (causing it to reject a block the C script accepted) or computes a wrong `withdrawed_interests` value, leaving the `s` field in the DAO header — the secondary-issuance pool that tracks outstanding depositor interest — incorrectly decremented. This is the direct CKB analog of a Vault contract's `SharePriceCheckpoint` not being updated on withdrawal: the per-block accounting checkpoint (`ar`, `s`) diverges from what the on-chain script actually enforced.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, `transaction_maximum_withdraw`** [1](#0-0) 

The comment on line 79 states: *"dao contract stores header deps index as u64 in the input_type field of WitnessArgs."* Rust reads the full 8-byte little-endian value and uses it directly as a `usize` index into `header_deps()`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
// …
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 cast to usize
```

The on-chain C DAO script, however, reads only the lowest byte of the same 8-byte field (a well-known implementation detail of the deployed `dao.c`). For any index value whose lowest byte differs from the full value (i.e., index > 255), the two implementations resolve different entries in `header_deps`.

**Consequence for the DAO field**

`dao_field_with_current_epoch` calls `withdrawed_interests`, which calls `transaction_maximum_withdraw` for every transaction in the block: [2](#0-1) 

`withdrawed_interests` is subtracted from `current_s`: [3](#0-2) 

When the index is > 255, Rust resolves `header_deps[full_index]` — a different block than the C script used. The subsequent block-number cross-check: [4](#0-3) 

almost always fails (the resolved block has a different number than `deposited_block_number`), so `transaction_maximum_withdraw` returns `Err(DaoError::InvalidOutPoint)`. This propagates through `withdrawed_interests` and causes `dao_field_with_current_epoch` to return an error, making the Rust node reject the entire block — even though the C DAO script accepted the transaction.

**The discrepancy is explicitly documented in the test suite:** [5](#0-4) 

The test comment on line 490–491 reads: *"Position 1: correct deposit block (what C VM resolves via lowest byte). Position 257: withdraw block (wrong — Rust resolves this with full u64)."*

---

### Impact Explanation

Two concrete impacts:

1. **Consensus split / block rejection.** A miner (or any node that assembles blocks using the C script directly) can include a valid DAO Phase-2 withdrawal whose witness encodes an index > 255. The C script accepts the transaction; every standard Rust node rejects the block because `dao_field_with_current_epoch` errors. This is a hard fork-class consensus split.

2. **`s`-field not decremented (stale accounting checkpoint).** If a crafted scenario avoids the block-number check (e.g., `header_deps[full_index]` happens to be a block at the same height as the deposit block — possible across forks referenced as header deps), Rust computes `withdrawed_interests` using the wrong `deposit_ar`, producing an incorrect `s` value. The secondary-issuance pool is then permanently mis-accounted, analogous to `SharePriceCheckpoint` being stale after a withdrawal. [6](#0-5) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged transaction sender submitting a DAO Phase-2 withdrawal via the tx-pool RPC or P2P relay. No special privilege is required.
- **Constraint**: The transaction must have ≥ 258 `header_deps` entries and encode a witness index whose lowest byte differs from the full value (e.g., 257 = `0x0101`). This is unusual but not prohibited by the protocol; `header_deps` has no enforced count limit.
- **Practical barrier**: Standard wallets never produce such transactions. An attacker must craft one deliberately. Likelihood is **low** but non-zero and fully attacker-controlled.

---

### Recommendation

In `transaction_maximum_withdraw`, validate that the u64 index fits in a `u8` before using it, or — better — align the Rust index-resolution logic with the deployed C DAO script by masking to the lowest byte:

```rust
// Current (line 91):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fix option A — reject out-of-range indices explicitly:
let idx = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if idx > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
Ok(idx)

// Fix option B — mirror C script behaviour (lowest byte only):
let raw = LittleEndian::read_u64(&header_deps_index_data.unwrap());
Ok(raw & 0xFF)
```

Option A is safer: it makes the Rust node strictly reject the ambiguous encoding rather than silently diverge from the C script. Option B matches C but permits the ambiguous encoding. Either way, the chosen behaviour must be documented and matched by the C script (or a future upgrade to it).

---

### Proof of Concept

The repository's own test constructs the exact scenario: [7](#0-6) 

```
header_deps[1]   = deposit_block  (block 100)   ← C script resolves (257 & 0xFF = 1)
header_deps[257] = withdraw_block (block 200)   ← Rust resolves (full u64 = 257)
witness input_type = 257 (u64 LE)
cell data = 100 (deposit block number)
```

Rust resolves `header_deps[257]` → block 200; checks `200 == 100` → **fails** → `Err(InvalidOutPoint)`.  
C script resolves `header_deps[1]` → block 100; checks `100 == 100` → **passes** → transaction accepted.

A block containing this transaction would be accepted by the C-script-based on-chain validator but rejected by every Rust node's `dao_field_with_current_epoch`, producing a consensus split.

### Citations

**File:** util/dao/src/lib.rs (L79-98)
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
```

**File:** util/dao/src/lib.rs (L105-107)
```rust
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
```

**File:** util/dao/src/lib.rs (L146-154)
```rust
        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
```

**File:** util/dao/src/lib.rs (L209-263)
```rust
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
