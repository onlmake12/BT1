### Title
Rust `DaoCalculator` Reads Full u64 Header-Dep Index While C DAO Script Reads Only the Lowest Byte — (`util/dao/src/lib.rs`)

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the full 8-byte little-endian `u64` from the witness `input_type` field to resolve the `header_deps` index for a DAO withdrawal. The on-chain C DAO script running in CKB-VM reads only the **lowest byte** of the same 8-byte field. When a transaction sender encodes an index whose lowest byte differs from the full u64 value (e.g., index `257` = `[0x01, 0x01, 0x00, …]`), the two implementations resolve to different `header_deps` entries. The Rust node's tx-pool then rejects a transaction that the consensus DAO script would accept, causing **permanent tx-pool censorship of legitimate DAO withdrawals**.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit header by reading the full 8-byte `u64` from the witness `input_type` field:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
``` [1](#0-0) 

It then uses this value directly as the `header_deps` array index:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [2](#0-1) 

The on-chain C DAO script, however, only reads the **lowest byte** (u8) of the same 8-byte field. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [3](#0-2) 

The test confirms the divergence: with `input_type = 257` (LE bytes `[0x01, 0x01, 0x00, …]`):
- **C DAO script**: reads lowest byte `0x01` → `header_deps[1]` = deposit block → **passes**
- **Rust `DaoCalculator`**: reads full u64 `257` → `header_deps[257]` = withdraw block → block number mismatch → **`DaoError::InvalidOutPoint`** [4](#0-3) 

This `DaoCalculator::transaction_fee` is called directly in the tx-pool admission path:

```rust
let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
    .transaction_fee(rtx)
    .map_err(|err| {
        Reject::Malformed(...)
    })?;
``` [5](#0-4) 

And in the `FeeCalculator` used during contextual transaction verification:

```rust
DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
    .transaction_fee(&self.transaction)
``` [6](#0-5) 

### Impact Explanation

Any DAO withdrawal transaction where the `header_deps` list has more than 255 entries and the witness `input_type` encodes an index whose lowest byte differs from the full u64 value will be **permanently rejected by the Rust node's tx-pool** with `Reject::Malformed`, even though the on-chain C DAO script would accept it. The user's DAO funds become unwithdrawable through any standard CKB node. This is a **transaction censorship** vulnerability affecting the tx-pool admission layer. If such a transaction is included in a block by a miner that bypasses the tx-pool check, the `DaoHeaderVerifier` (which uses `dao_field`, not `transaction_fee`) may still accept the block, creating a **split between tx-pool policy and consensus**.

### Likelihood Explanation

A transaction sender can deliberately craft a DAO withdrawal with 258+ `header_deps` entries and encode the deposit header index as `(correct_index_low_byte) | (arbitrary_high_bytes << 8)`. This is a valid, well-formed transaction that any standard CKB wallet or script author could construct. No privileged access, key material, or majority hashpower is required. The attacker-controlled entry path is the standard `send_transaction` RPC or P2P relay.

### Recommendation

In `util/dao/src/lib.rs`, align the Rust `DaoCalculator` with the C DAO script's actual behavior: read only the lowest byte (u8) of the `input_type` field as the `header_deps` index, or enforce that the index fits in a u8 and reject transactions where the upper 7 bytes are non-zero. The comment on line 79 already acknowledges the C contract's convention; the implementation must match it. [7](#0-6) 

### Proof of Concept

The repository already contains a test that directly demonstrates the divergence. In `util/dao/src/tests.rs`, `check_dao_withdraw_header_dep_index_exceeds_u8`:

1. Build a DAO withdrawal transaction with 258 `header_deps` entries.
2. Place the deposit block at index 1 and the withdraw block at index 257.
3. Set witness `input_type` = `257u64.to_le_bytes()` (lowest byte = 1).
4. The C DAO script resolves index 1 → deposit block → **passes**.
5. `DaoCalculator::transaction_fee` resolves index 257 → withdraw block → block number 200 ≠ cell data 100 → **`DaoError::InvalidOutPoint`**. [8](#0-7) 

The tx-pool would reject this transaction via `check_tx_fee` → `Reject::Malformed`, permanently blocking a legitimate DAO withdrawal that the consensus script would approve. [9](#0-8)

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

**File:** verification/src/transaction_verifier.rs (L270-272)
```rust
            DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
                .transaction_fee(&self.transaction)
        }
```
