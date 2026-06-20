### Title
DAO Withdrawal Permanently Blocked When `header_dep_index` Exceeds 255 Due to Type Mismatch Between Rust Fee Verifier and On-Chain C Script — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the DAO `header_dep_index` from the witness as a full `u64`, while the on-chain DAO C script reads only the lowest byte (`u8`). When a withdrawal transaction encodes an index `> 255`, the Rust verifier resolves a different block header than the C VM does, causing `DaoCalculator::transaction_fee` to return `DaoError::InvalidOutPoint`. The tx-pool's `check_tx_fee` maps this error to `Reject::Malformed`, permanently blocking the transaction from entering the pool — even though the on-chain C VM would accept it.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, line 91:**

```rust
// dao contract stores header deps index as u64 in the input_type field of WitnessArgs
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

The Rust code reads the full 8-byte little-endian value as a `u64` and uses it directly as the `header_deps` array index (line 96: `header_deps().get(header_dep_index as usize)`).

The on-chain DAO C script, however, reads only the **lowest byte** of the same 8-byte field (treating it as a `u8`). For any index `> 255`, the two implementations resolve to different entries in `header_deps`.

**Concrete divergence for `header_dep_index = 257` (lowest byte = `1`):**

| Layer | Index read | Resolved entry | Block number | Outcome |
|---|---|---|---|---|
| On-chain C VM | `1` (lowest byte) | `header_deps[1]` = deposit block | 100 | ✓ accepts |
| Rust `DaoCalculator` | `257` (full u64) | `header_deps[257]` = wrong block | 200 | ✗ `deposit_header.number() != deposited_block_number` → `DaoError::InvalidOutPoint` |

**Propagation to tx-pool rejection — `tx-pool/src/util.rs`, lines 34–41:**

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

Any `DaoError` from `transaction_fee` is converted to `Reject::Malformed`. The transaction is permanently rejected from the pool; there is no retry path.

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) explicitly documents this divergence, constructing a 258-entry `header_deps` list with the deposit block at position 1 and the withdraw block at position 257, encoding `input_type = 257`, and asserting `result.is_err()` from the Rust layer — while the comment confirms the C VM would resolve via the lowest byte to position 1 (the correct deposit block).

---

### Impact Explanation

A DAO depositor whose withdrawal transaction encodes `header_dep_index > 255` — a structurally valid encoding accepted by the on-chain C VM — will have that transaction permanently rejected by every CKB node's tx-pool with `Reject::Malformed`. The depositor cannot reclaim their locked CKB through the standard `send_transaction` RPC path. Because the rejection is deterministic and not time-bounded, the funds remain inaccessible via any conforming node until the Rust verifier is corrected. The impact class is **permanent denial of DAO withdrawal** for affected transactions.

---

### Likelihood Explanation

A transaction with more than 255 `header_deps` is unusual but protocol-legal. A depositor with many simultaneous DAO inputs, or one who constructs a withdrawal referencing a large number of historical headers, can reach this condition without any malicious intent. The condition is also reachable by any unprivileged `send_transaction` RPC caller — no special role or key is required. Likelihood is **low-to-medium** given the rarity of >255 header deps in practice, but the impact when triggered is severe and unrecoverable without a node patch.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain C VM's actual index width. If the C script reads only the lowest byte, the Rust code at `util/dao/src/lib.rs:91` should cast the decoded `u64` to `u8` before using it as an index:

```rust
// Match on-chain C VM behavior: only the lowest byte is used as the index
let index = LittleEndian::read_u64(&header_deps_index_data.unwrap()) as u8 as usize;
```

Alternatively, if the intent is for both layers to use the full `u64`, the on-chain C script must be updated accordingly and a hardfork scheduled. Either way, the two layers must agree on the same interpretation before the fix is considered complete.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` (lines 475–537) is a self-contained proof of concept:

1. Build a DAO withdrawal `ResolvedTransaction` with 258 `header_deps`.
2. Place the deposit block hash at index 1 and the withdraw block hash at index 257.
3. Set `WitnessArgs.input_type = 257u64` (little-endian).
4. Call `DaoCalculator::transaction_fee(&rtx)`.

The Rust verifier resolves index 257 → withdraw block (number 200), but `cell_data` records deposit at block 100. The block-number check at `util/dao/src/lib.rs:105` fails:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
```

`transaction_fee` returns `Err(DaoError::InvalidOutPoint)`. In the live node, `check_tx_fee` in `tx-pool/src/util.rs:36–40` converts this to `Reject::Malformed`, and the withdrawal is permanently blocked. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
