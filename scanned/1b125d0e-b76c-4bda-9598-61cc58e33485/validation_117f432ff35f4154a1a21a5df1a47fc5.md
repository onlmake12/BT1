### Title
DAO Withdrawal Interest Calculated with Wrong Deposit Header Due to u64/u8 Witness-Index Mismatch — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the witness `input_type` field as a full **u64** to index into `header_deps`, while the on-chain C DAO script (running inside CKB-VM) reads only the **lowest byte (u8)** of the same field. When a transaction sender supplies a witness index > 255, the two implementations resolve different `header_dep` entries, causing them to use different accumulation-rate (`ar`) values for the DAO interest calculation. This is the direct CKB analog of H-26: the wrong index is used to look up the parameter that drives the interest/capacity calculation.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` parses the deposit-header index from the witness like this:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))  // line 91
```

and then uses it to fetch the deposit header:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // line 96
```

The on-chain C DAO script (referenced in the codebase at `test/src/specs/dao/dao_user.rs` line 14 as `https://github.com/nervosnetwork/ckb-system-scripts/…/c/dao.c#L81`) reads only the **lowest byte** of the same 8-byte little-endian field. This discrepancy is explicitly documented in the test suite:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
```

**Attack construction:**

A transaction sender crafts a DAO withdrawal transaction where:

| slot | content |
|---|---|
| `header_deps[1]` | fork block F at height H, with accumulation rate `ar_F` (lower) |
| `header_deps[257]` | canonical deposit block D at height H, with accumulation rate `ar_D` (higher) |
| cell data | `H` (deposit block number) |
| witness `input_type` | `257` (0x0000000000000101 LE) |

- **C VM** reads lowest byte → index 1 → block F → `ar_F` → computes interest with `ar_F`
- **Rust** reads full u64 → index 257 → block D → `ar_D` → computes interest with `ar_D`

Both blocks have the same block number H, so the block-number guard at line 105 (`deposit_header.number() != deposited_block_number`) passes in **both** implementations. The two implementations silently diverge on the `ar` value used.

The `withdrawed_interests` fed into `dao_field_with_current_epoch` (line 222) is computed by Rust using `ar_D`, while the on-chain script actually enforced `ar_F`. The resulting `current_s` field packed into the block's DAO field is therefore wrong, and any node that re-verifies the block independently will compute a different DAO field and reject the block. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

1. **Incorrect DAO field / consensus split.** `withdrawed_interests` is computed with the wrong `ar`, so `current_s` in the packed DAO field diverges from what the on-chain script enforced. Verifying nodes recompute the DAO field independently and reject the block → chain split.

2. **False rejection of valid transactions.** In the simpler case (test scenario), Rust resolves index 257 to a block whose number does not match the cell data, so it rejects a transaction the on-chain script would accept. This breaks tx-pool admission and block verification for legitimate users.

3. **Incorrect fee accounting.** `transaction_fee` (line 30) calls `transaction_maximum_withdraw`, so fee calculations for DAO withdrawal transactions are wrong whenever the index exceeds 255. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

- **Attacker-controlled entry path**: any transaction sender submitting a DAO withdrawal transaction via RPC or P2P relay. No privileged role required.
- **Preconditions**: (a) the node's database contains two blocks at the same height with different `ar` values (normal during any fork/reorg); (b) the transaction has ≥ 258 `header_deps` entries — each is 32 bytes, so 258 entries = ~8 KB, well within CKB's block-size limit.
- **No trusted role, no majority hashpower, no social engineering** required. [6](#0-5) 

---

### Recommendation

Replace the full-u64 read with a u8 read to match the on-chain C script's behavior:

```rust
// Before (line 91):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After — match the C VM's lowest-byte semantics:
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add an explicit rejection of any witness index whose upper 7 bytes are non-zero, so that the Rust node and the C VM are guaranteed to agree on the resolved `header_dep` entry. [7](#0-6) 

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) already demonstrates the divergence:

- `header_deps[1]` = deposit block (number 100) — what the C VM resolves
- `header_deps[257]` = withdraw block (number 200) — what Rust resolves
- Witness `input_type` = 257 (lowest byte = 1)
- Rust rejects with a block-number mismatch; the C VM would accept

To demonstrate the silent `ar`-divergence variant (both accept, different interest), replace `header_deps[257]` with a fork block at height 100 carrying a different `ar` value. Both the block-number check and the `header_deps` membership check pass in both implementations, but `calculate_maximum_withdraw` uses a different `deposit_ar` in each, producing a different withdrawal capacity and a different `withdrawed_interests` contribution to the DAO field. [8](#0-7) [9](#0-8)

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

**File:** util/dao/src/lib.rs (L83-99)
```rust
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

**File:** util/dao/src/lib.rs (L101-113)
```rust
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

**File:** util/dao/src/lib.rs (L208-222)
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
