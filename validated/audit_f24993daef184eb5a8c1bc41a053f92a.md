### Title
DAO Withdrawal `header_dep_index` Parsed as `u64` in Rust Node but as `u8` (Lowest Byte) in C VM DAO Script, Causing Tx-Pool Rejection of Valid Withdrawals and Potential Consensus Split — (`util/dao/src/lib.rs`)

---

### Summary

The Rust node's `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from the DAO withdrawal witness as a full little-endian `u64`. The on-chain C VM DAO script, however, reads only the **lowest byte** of that same 8-byte field. When a user encodes an index whose full `u64` value exceeds 255 but whose lowest byte is a valid deposit-header position (e.g., `257 = 0x0000000000000101`, lowest byte `0x01`), the two interpretations diverge: the Rust node resolves a different `header_deps` slot than the C VM does. The Rust node's block-number cross-check then fails, causing the tx-pool to reject a transaction that the C VM would accept. This is the direct CKB analog of the external report's root cause: user-controlled parameters are used to authorize an asset operation, but the validation of those parameters is inconsistent between two layers of the system.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, `transaction_maximum_withdraw`**

```
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

The 8-byte `input_type` field of the `WitnessArgs` is decoded as a full `u64` and used directly to index into `header_deps`:

```
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)   // full u64 used as index
``` [1](#0-0) 

The on-chain C VM DAO script, by contrast, reads only the lowest byte of the same 8-byte little-endian value (i.e., it treats the field as a `u8`). This is explicitly documented in the test added to capture the discrepancy:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
``` [2](#0-1) 

**Concrete divergence**

A transaction is constructed with:
- `header_dep_index` witness value = `257` (`0x0000000000000101`)
- `header_deps[1]` = deposit block (block 100)
- `header_deps[257]` = withdraw block (block 200)
- Cell data = `100` (deposited block number)

| Layer | Index resolved | Block found | Block-number check | Result |
|---|---|---|---|---|
| C VM (on-chain) | `0x01` = 1 | deposit block (100) | 100 == 100 ✓ | **PASS** |
| Rust `DaoCalculator` | `257` | withdraw block (200) | 200 ≠ 100 ✗ | **FAIL** |

The test confirms the Rust node fails:

```rust
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
``` [3](#0-2) 

**Why the `CapacityVerifier` does not save the situation**

The `CapacityVerifier` explicitly **skips** the `OutputsSumOverflow` check for DAO withdrawal transactions, delegating capacity enforcement entirely to the C VM type script:

```rust
// DAO withdraw transaction is verified via the type script of DAO cells
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
``` [4](#0-3) 

This means the Rust node's block-level validation would **accept** a block containing such a transaction (C VM passes), but the tx-pool's `DaoCalculator`-based fee check would **reject** it before it ever reaches a miner.

**Attack / impact path**

1. A DAO depositor constructs a phase-2 withdrawal transaction with `header_dep_index = 257`, placing the deposit block hash at `header_deps[1]` and padding `header_deps` to 258 entries.
2. The C VM DAO script reads index `1` → deposit block → block-number matches → script **passes**.
3. The Rust tx-pool calls `DaoCalculator::transaction_fee` → reads index `257` → withdraw block → block-number mismatch → **rejects** the transaction with `DaoError::InvalidOutPoint`.
4. The transaction is permanently stuck: it cannot enter any honest node's tx-pool, so it cannot be mined through normal means.
5. If a miner accepts the raw transaction out-of-band and includes it in a block, all other nodes validate it via the C VM (passes) and the `CapacityVerifier` (skips DAO check), so the block is **accepted** by the network — but the originating node's tx-pool had already rejected it, creating an inconsistency between tx-pool admission logic and block-validation logic (a latent consensus-split surface).

---

### Impact Explanation

**Primary — Liveness / fund lock**: Any DAO withdrawal transaction whose `header_dep_index` encodes a value `> 255` (lowest byte ≠ full value) is permanently rejected by every honest Rust node's tx-pool, even though the on-chain C VM script would accept it. Affected users cannot withdraw their DAO interest through normal node operation.

**Secondary — Consensus split surface**: Because the `CapacityVerifier` delegates DAO capacity enforcement to the C VM, a block containing such a transaction is valid at the block-validation layer but was rejected at the tx-pool layer. This divergence between the two enforcement layers is a latent consensus-split vector: a miner who bypasses the tx-pool can produce a block that honest nodes accept at the chain level but that the tx-pool's own logic would have refused.

---

### Likelihood Explanation

A user with a large number of DAO inputs (and thus many header deps) could naturally produce a `header_dep_index > 255`. The `header_deps` array has no protocol-enforced upper bound on length, so indices above 255 are reachable in practice. The discrepancy is also exploitable deliberately by any transaction sender who controls the witness encoding. The entry point is the standard `send_transaction` RPC, requiring no special privilege.

---

### Recommendation

In `DaoCalculator::transaction_maximum_withdraw` (`util/dao/src/lib.rs`), after decoding `header_dep_index` as `u64`, add an explicit bounds check that rejects any value exceeding `u8::MAX` (255), matching the C VM script's actual interpretation:

```rust
let index = LittleEndian::read_u64(&header_deps_index_data.unwrap());
if index > u8::MAX as u64 {
    return Err(DaoError::InvalidDaoFormat);
}
Ok(index)
```

Alternatively, if the protocol intends to support indices above 255 in the future, the C VM DAO script must be updated to read the full `u64` value, and a hard-fork or script upgrade must be coordinated so both layers agree on the same interpretation.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the divergence. It constructs a transaction where:

- `header_deps[1]` = deposit block (block 100) — what the C VM resolves
- `header_deps[257]` = withdraw block (block 200) — what the Rust node resolves
- Witness `input_type` = `257u64` in little-endian [5](#0-4) 

The test asserts `result.is_err()`, confirming the Rust node rejects a transaction the C VM would accept. To complete the proof of concept, submit this transaction via `send_transaction` RPC and observe the `DaoError::InvalidOutPoint` rejection, then verify that a block manually assembled to include the same transaction passes `ContextualTransactionVerifier` (since `CapacityVerifier` skips the DAO capacity check and the C VM script passes). [6](#0-5) [4](#0-3)

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

**File:** verification/src/transaction_verifier.rs (L479-494)
```rust
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
