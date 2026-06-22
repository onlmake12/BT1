### Title
`DaoCalculator` reads full u64 header-deps index while DAO type script (C VM) resolves only the lowest byte, causing valid DAO withdrawals to be permanently rejected — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::transaction_maximum_withdraw` reads the `input_type` witness field as a full little-endian `u64` to index into `header_deps`. The on-chain DAO type script (C VM) resolves the same field using only its lowest byte. When a transaction encodes an index whose full u64 value and lowest-byte value differ (e.g., `257` → full = 257, lowest byte = 1), the Rust node resolves a different header than the C VM does. The Rust node then fails a block-number consistency check and permanently rejects the transaction, while the on-chain script accepts it. This is a consensus split: a block that is valid per on-chain consensus is rejected by every Rust node.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the deposit-header index from the witness `input_type` field:

```rust
// dao contract stores header deps index as u64 in the input_type field of WitnessArgs
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
``` [1](#0-0) 

It then uses that full `u64` to index `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [2](#0-1) 

The resolved header's block number is then compared against the cell data:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
``` [3](#0-2) 

The DAO type script running in CKB-VM resolves the same `input_type` field using only its **lowest byte**. For a witness encoding `257` (little-endian `0x0101_0000_0000_0000`):

| Layer | Index resolved | `header_deps[index]` |
|---|---|---|
| C VM (on-chain) | `1` (lowest byte) | deposit block ✓ |
| Rust `DaoCalculator` | `257` (full u64) | wrong block ✗ |

The Rust node resolves a different header, the block-number check fails, `transaction_fee` returns `DaoError::InvalidOutPoint`, and the transaction (and any block containing it) is permanently rejected.

This discrepancy is explicitly documented in the test suite:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
// input_type = 257, lowest byte = 1
``` [4](#0-3) 

The test confirms the Rust path returns `Err`: [5](#0-4) 

---

### Impact Explanation

`DaoCalculator::transaction_fee` is called during both tx-pool admission (`tx-pool/src/util.rs:check_tx_fee`) and contextual block verification (`verification/src/transaction_verifier.rs:FeeCalculator`). [6](#0-5) [7](#0-6) 

A miner who includes a crafted DAO withdrawal in a block produces a block that:
- **passes** on-chain script execution (C VM accepts the transaction), and
- **fails** Rust-node block verification (`DaoError::InvalidOutPoint` → `InvalidRewardAmount` or contextual tx error).

Every Rust node permanently rejects the block, splitting the chain. Additionally, any user who submits such a DAO withdrawal via RPC will have it silently rejected at the tx-pool stage, making their funds unwithdrawable through the Rust node.

---

### Likelihood Explanation

The attacker must craft a DAO withdrawal transaction with ≥ 258 `header_deps` entries and set `input_type` to a value whose lowest byte points to the correct deposit block while the full u64 points elsewhere. This is fully under the control of an unprivileged transaction sender or miner. No special privilege, key material, or majority hash power is required. The transaction structure is valid per the Molecule schema (`header_deps: Byte32Vec` is unbounded).

---

### Recommendation

Replace the full-u64 read with a read that matches the DAO type script's interpretation. If the C VM reads only the lowest byte, the Rust code should do the same:

```rust
// Read only the lowest byte to match DAO type script (C VM) behavior
let index_byte = header_deps_index_data.unwrap()[0];
Ok(index_byte as u64)
```

Alternatively, if the intent is to support a full u64 index, the DAO type script must be updated to match, and a hard-fork coordinated. The two layers must agree on the same interpretation.

---

### Proof of Concept

1. Construct a DAO withdrawal transaction with 258 `header_deps`:
   - `header_deps[1]` = hash of the original deposit block (block 100)
   - `header_deps[257]` = hash of any other block (e.g., block 200)
   - All other entries = dummy hashes
2. Set the witness `input_type` to `257u64` encoded as 8 little-endian bytes (`0x01, 0x01, 0x00, ...`).
3. The DAO type script (C VM) reads lowest byte = `1`, resolves `header_deps[1]` = deposit block (number 100), matches cell data → **accepts**.
4. `DaoCalculator::transaction_maximum_withdraw` reads full u64 = `257`, resolves `header_deps[257]` = block 200, `deposit_header.number()` (200) ≠ `deposited_block_number` (100) → returns `DaoError::InvalidOutPoint` → **rejects**.
5. Any block containing this transaction is rejected by all Rust nodes, causing a consensus split. [8](#0-7)

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

**File:** util/dao/src/lib.rs (L105-107)
```rust
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
```

**File:** util/dao/src/tests.rs (L489-514)
```rust
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
```

**File:** util/dao/src/tests.rs (L530-536)
```rust
    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.transaction_fee(&rtx);

    // Rust resolves index 257 → withdraw block (number 200), but cell data
    // says deposited at block 100. Block number check catches the mismatch.
    assert!(result.is_err(), "expected Err, got {result:?}");
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
