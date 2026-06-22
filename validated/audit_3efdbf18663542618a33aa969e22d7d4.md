### Title
DAO Withdrawal Header-Dep Index Width Mismatch Between Rust `DaoCalculator` and On-Chain C Script — (`util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the deposit-header index from the witness `input_type` field as a full 8-byte little-endian `u64`, while the on-chain C DAO script (`dao.c`) reads the same field as a `uint8_t` (lowest byte only). When a transaction author encodes an index value whose lowest byte differs from the full value (i.e., any index > 255), the Rust node resolves a **different** deposit header than the C script does. This is a direct analog to the ERC1155A bug: the wrong party's (wrong header's) accounting is used, causing the Rust node to compute a different maximum-withdraw capacity than the on-chain script enforces.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the header-dep index from the witness like this:

```rust
// line 91
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

then immediately uses it as an array index:

```rust
// lines 94-98
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The comment on line 79 states: *"dao contract stores header deps index as u64 in the input_type field of WitnessArgs"* — but the actual on-chain C DAO script reads this field as a `uint8_t`, consuming only the first (lowest) byte. For any index value whose full u64 representation differs from its lowest byte (i.e., any value > 255), the two sides resolve **different** entries in `header_deps`.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) explicitly demonstrates this: with a witness index of 257 (little-endian bytes `[0x01, 0x01, 0x00, …]`), the C script reads byte 0 = 1 and resolves `header_deps[1]`, while the Rust code reads the full u64 = 257 and resolves `header_deps[257]`.

`DaoCalculator` is called in two critical production paths:
- **Tx-pool admission**: `tx-pool/src/util.rs` → `check_tx_fee` → `DaoCalculator::transaction_fee`
- **Block verification**: `verification/contextual/src/contextual_block_verifier.rs` → `DaoCalculator`

---

### Impact Explanation

**Scenario A — Valid withdrawal rejected from tx-pool (DoS)**:
A legitimate DAO depositor crafts a withdrawal with > 255 `header_deps`. The C script resolves the correct deposit header via the lowest byte of the index. The Rust `DaoCalculator` resolves a different (wrong) header at the full u64 index, causing `deposit_header.number() != deposited_block_number` (line 105–106) and returning `DaoError::InvalidOutPoint`. The tx-pool rejects the transaction as having an invalid fee, even though the on-chain script would accept it. The depositor cannot withdraw their funds through this node.

**Scenario B — Consensus split via block verifier**:
Because `DaoCalculator` is also invoked in `verification/contextual/src/contextual_block_verifier.rs`, a block containing a valid DAO withdrawal (accepted by the C script using the lowest-byte index) may be rejected by the Rust block verifier (which resolves a different header via the full u64 index). This causes the Rust node to reject a chain-valid block, splitting from peers that correctly accepted it.

---

### Likelihood Explanation

A transaction with more than 255 `header_deps` is unusual in normal usage (typical DAO withdrawals have 2), but it is not protocol-prohibited. A malicious transaction submitter or a script author targeting the discrepancy can deliberately craft such a transaction. The attacker-controlled entry path is the standard `send_transaction` RPC or P2P relay — no privileged access is required. The root cause is entirely within the Rust node's own production code.

---

### Recommendation

Change the index-reading logic in `transaction_maximum_withdraw` to consume only the lowest byte, matching the C script's behavior:

```rust
// Before (line 91):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// After — read as u8 to match dao.c's uint8_t cast:
Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, add an explicit bounds check rejecting any index whose full u64 value exceeds 255, so the two sides are guaranteed to agree.

---

### Proof of Concept

1. Construct a DAO withdrawal transaction with 258 `header_deps`:
   - `header_deps[1]` = `deposit_block.hash()` (the correct deposit block, block number 100)
   - `header_deps[257]` = `withdraw_block.hash()` (block number 200)
   - All other slots = dummy hashes
2. Set the witness `input_type` to `257u64` in little-endian (`[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`).
3. Set cell data to `100u64` (the deposit block number).
4. **C script** reads byte 0 = `1` → resolves `header_deps[1]` = deposit block (number 100) → matches cell data → **accepts**.
5. **Rust `DaoCalculator`** reads full u64 = `257` → resolves `header_deps[257]` = withdraw block (number 200) → `200 != 100` → returns `DaoError::InvalidOutPoint` → **rejects**.

This exact scenario is encoded in the existing test: [1](#0-0) 

The root cause is at: [2](#0-1) 

The `DaoCalculator` is consumed in the block verification path at: [3](#0-2) 

and in the tx-pool fee check at: [4](#0-3)

### Citations

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

**File:** util/dao/src/lib.rs (L91-99)
```rust
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L1-2)
```rust
use crate::uncles_verifier::{UncleProvider, UnclesVerifier};
use ckb_async_runtime::Handle;
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
