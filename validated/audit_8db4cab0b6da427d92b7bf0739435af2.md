### Title
DAO Withdrawal Witness Index Parsed as u64 by Rust but u8 by C Script, Bypassing Block-Number Guard with Same-Height Fork Blocks — (File: util/dao/src/lib.rs)

### Summary
`DaoCalculator::transaction_maximum_withdraw` in `util/dao/src/lib.rs` reads the header-deps index stored in the DAO withdrawal witness as a full `u64`, while the on-chain C DAO script (`dao.c`) reads only the lowest byte (`u8`). A partial block-number guard exists but only catches cases where the two resolved blocks have *different* block numbers. An attacker can craft a withdrawal transaction whose witness encodes an index `X > 255` such that `header_deps[X & 0xFF]` and `header_deps[X]` both point to blocks at the **same height** (e.g., a canonical block and a fork/uncle block at that height) but with different accumulation-rate (AR) values. Rust resolves the higher-AR block and computes an inflated maximum-withdraw; the C script resolves the lower-AR block and rejects the transaction. The result is tx-pool admission of a transaction that is invalid under script execution, and a miner who includes it produces an invalid block.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness `input_type` field:

```rust
// line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses this full `u64` to index into `header_deps`:

```rust
// lines 93-99
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
        ...
```

The C DAO script (`dao.c`, referenced in the test at `util/dao/src/tests.rs:490`) reads only the **lowest byte** of the same 8-byte field. The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` explicitly documents this divergence:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

The only guard against this divergence is the block-number check at line 105:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
```

This check is **insufficient**: it only rejects cases where the two resolved blocks have *different* block numbers. It does not reject the case where `header_deps[X & 0xFF]` and `header_deps[X]` are two distinct blocks at the **same height** — which is a normal occurrence on any chain that has experienced forks or uncle blocks.

Attack construction:
1. Let `X = 256` (0x100). Lowest byte = 0, full u64 = 256.
2. Set `header_deps[0]` = block A at height H (canonical deposit block, lower AR).
3. Set `header_deps[256]` = block B at height H (a fork block at the same height, higher AR).
4. Set cell data = `H.to_le_bytes()` (deposited block number).
5. Set witness `input_type` = `256u64.to_le_bytes()`.
6. Rust resolves index 256 → block B → `deposit_header.number() == H` → guard passes → computes inflated `maximum_withdraw` using block B's higher AR.
7. C script resolves index 0 → block A → computes lower `maximum_withdraw` using block A's lower AR.
8. Set output capacity = Rust-computed (inflated) amount.
9. Rust's `check_tx_fee` accepts the transaction (fee ≥ 0).
10. C script rejects the transaction (output > C-computed maximum withdraw).

`CapacityVerifier` provides no backstop because it explicitly skips the capacity overflow check for all DAO withdrawal transactions, delegating entirely to the C script:

```rust
// verification/src/transaction_verifier.rs line 483
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    // capacity check skipped for DAO withdrawals
```

### Impact Explanation

**Impact: High**

- **Tx-pool pollution**: Any unprivileged transaction sender can submit a crafted DAO withdrawal that passes Rust's fee gate (`check_tx_fee` via `DaoCalculator::transaction_fee`) but is rejected by the C DAO script at execution time. The transaction occupies tx-pool slots and propagates to peers.
- **Miner block invalidation**: If a miner's block assembler selects the poisoned transaction (the assembler does not re-run full script execution during `calc_dao`), the assembled block will be rejected by all verifying nodes when the C DAO script executes, causing the miner to waste their PoW work.
- **Fee accounting divergence**: `withdrawed_interests` in `dao_field_with_current_epoch` also calls `transaction_maximum_withdraw`, so the DAO field computed for a block template that includes such a transaction will be wrong, further invalidating the block.

### Likelihood Explanation

**Likelihood: Low**

The attacker must:
1. Obtain the hash of a fork/uncle block at the same height as the deposit block — this is available from any node that has seen a chain reorganization or uncle blocks, which is routine on mainnet.
2. Craft a transaction with ≥ 257 `header_deps` entries — unusual but not prohibited by any consensus rule.
3. Submit the transaction to a node's tx-pool via the standard `send_transaction` RPC — no privileged access required.

The constraint that two blocks at the same height must have meaningfully different AR values is easily satisfied: AR accumulates with every block, so any two blocks at the same height on different fork branches will have different AR values.

### Recommendation

Replace the full-u64 index read with a u8 read to match the C DAO script's behavior, or add an explicit check that rejects any witness index whose value exceeds 255:

```rust
// In transaction_maximum_withdraw, after reading header_deps_index_data:
let raw_index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if raw_index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
```

Alternatively, align the C DAO script to read the full u64 (requires a system-script upgrade). The fix must be applied consistently in both `transaction_maximum_withdraw` and any other Rust path that resolves the deposit header from the witness index.

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` demonstrates the divergence for the case where the two blocks have *different* numbers (caught by the guard). The uncaught case requires two blocks at the *same* height:

```rust
// Construct two blocks at the same height H with different AR values
let deposit_number = 100u64;
let epoch = EpochNumberWithFraction::new(1, 100, 1000);

// Block A: canonical deposit block, AR = 10_000_000_000_123_456
let block_a = BlockBuilder::default()
    .header(HeaderBuilder::default()
        .number(deposit_number)
        .epoch(epoch)
        .dao(pack_dao_data(10_000_000_000_123_456, ...))
        .build())
    .build();

// Block B: fork block at same height, AR = 10_000_000_002_000_000 (higher)
let block_b = BlockBuilder::default()
    .header(HeaderBuilder::default()
        .number(deposit_number)  // SAME height
        .epoch(epoch)
        .dao(pack_dao_data(10_000_000_002_000_000, ...))
        .build())
    .build();

// Pad header_deps: index 0 = block_a (C script), index 256 = block_b (Rust)
let mut header_deps = vec![dummy; 257];
header_deps[0]   = block_a.hash();  // C script reads lowest byte of 256 → 0
header_deps[256] = block_b.hash();  // Rust reads full u64 256

// Witness encodes index 256
let witness = WitnessArgs::new_builder()
    .input_type(Some(Bytes::from(256u64.to_le_bytes().to_vec())))
    .build();

// Cell data stores deposit_number (100) — matches BOTH blocks' .number()
// so the block-number guard at lib.rs:105 passes for Rust (block_b.number() == 100)

// Rust computes maximum_withdraw using block_b's higher AR → inflated result
// C script computes maximum_withdraw using block_a's lower AR → lower result
// Setting output capacity = Rust result → Rust accepts, C script rejects
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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
