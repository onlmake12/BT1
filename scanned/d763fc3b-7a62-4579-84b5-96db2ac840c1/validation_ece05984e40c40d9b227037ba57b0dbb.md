### Title
`DaoCalculator::transaction_maximum_withdraw` Reads `header_dep_index` as Full u64 While C DAO Script Reads Only Lowest Byte, Causing Consensus Discrepancy and Incorrect DAO Withdrawal Accounting — (`File: util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw` reads the `header_dep_index` from the DAO withdrawal witness as a full 8-byte little-endian u64. The on-chain C DAO script, however, reads only the lowest byte of that same 8-byte field. For any transaction where `header_dep_index > 255`, the two implementations resolve to different entries in `header_deps`, causing the Rust node to compute the wrong deposit header, reject a transaction the C script considers valid, and produce an incorrect DAO field for block-header verification. This is a direct analog to the external report's pattern: a correctly computed intermediate value is overridden by a subsequent step that uses a different interpretation of the same data, corrupting the accounting result.

---

### Finding Description

**Root cause — `util/dao/src/lib.rs`, line 91:**

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

The Rust `DaoCalculator` reads the full 8-byte witness field as a u64 and uses it as the array index into `header_deps`.

The on-chain C DAO script, as documented explicitly in the production test `check_dao_withdraw_header_dep_index_exceeds_u8` (`util/dao/src/tests.rs`, lines 489–491):

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

reads only the **lowest byte** of the same 8-byte field. For a witness containing `257u64` in little-endian (`[0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]`):

| Component | Reads | Resolves to | Result |
|---|---|---|---|
| C DAO script | byte 0 = `0x01` = 1 | `header_deps[1]` = deposit block (number 100) | ACCEPT |
| Rust `DaoCalculator` | full u64 = 257 | `header_deps[257]` = withdraw block (number 200) | block-number mismatch → REJECT |

**Propagation through the verification stack:**

`ContextualTransactionVerifier::verify()` (`verification/src/transaction_verifier.rs`, lines 162–171) runs both the C script and the Rust fee calculator:

```rust
self.script.verify(max_cycles)?          // C DAO script → PASSES
let fee = self.fee_calculator.transaction_fee()?;  // DaoCalculator → FAILS
```

The C script passes because it resolves the correct deposit header via the lowest byte. The Rust `FeeCalculator` then calls `DaoCalculator::transaction_fee` → `transaction_maximum_withdraw`, which resolves the wrong header (index 257 = withdraw block, number 200 ≠ cell data 100), triggering `DaoError::InvalidOutPoint`.

The same `DaoCalculator` is used in `DaoHeaderVerifier::verify()` (`verification/contextual/src/contextual_block_verifier.rs`, lines 300–319) to recompute the DAO field for every block. If a block contains a DAO withdrawal with `header_dep_index = 257`, the Rust node computes `withdrawed_interests` using the wrong deposit header, producing a wrong `current_s` value, and rejects the block with `InvalidDAO` — even though the C DAO script accepted the transaction.

**Attacker-controlled entry path:**

A transaction sender (unprivileged) submits a DAO withdrawal phase-2 transaction where:
- `header_deps` has ≥ 258 entries
- `header_deps[1]` = the real deposit block hash
- `header_deps[257]` = any other block hash
- Witness `input_type` = `257u64` in little-endian

No privileged access is required to craft or submit such a transaction.

---

### Impact Explanation

1. **Incorrect fee/capacity accounting in the tx-pool:** `check_tx_fee` in `tx-pool/src/util.rs` (line 34) calls `DaoCalculator::transaction_fee`. A valid DAO withdrawal (accepted by the C script) is rejected from the tx-pool with `Reject::Malformed`, making the user's DAO funds unwithdrawable via this transaction format.

2. **Incorrect DAO field in block verification:** If a miner includes such a transaction in a block (e.g., via a non-standard path), `DaoHeaderVerifier` computes the wrong `withdrawed_interests`, producing a wrong DAO field, and the Rust node rejects the block — a consensus split between nodes that run only the C script and nodes that also run the Rust `DaoCalculator`.

3. **Asymmetric `verify_with_pause` ordering:** In `verify_with_pause` (`verification/src/transaction_verifier.rs`, lines 177–189), `fee_calculator.transaction_fee()` is called **before** `script.resumable_verify_with_signal()`, meaning the Rust fee check gates execution even when the C script would succeed.

---

### Likelihood Explanation

Normal DAO withdrawals use `header_dep_index` values of 0 or 1 (only 2 header_deps are needed), so this discrepancy is not triggered in typical usage. However, any transaction sender can deliberately construct a withdrawal with 258+ header_deps and `header_dep_index = 257`. The construction requires no privileged access — only the ability to submit a transaction. The scenario is realistic for an attacker who wants to demonstrate a consensus discrepancy or block a specific DAO withdrawal.

---

### Recommendation

Align the Rust `DaoCalculator` with the C DAO script's actual byte-width interpretation. If the C script reads only the lowest byte, the Rust code should do the same:

```diff
- Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
+ Ok(u64::from(header_deps_index_data.unwrap()[0]))
```

Alternatively, if the C script should be reading the full u64 (and the comment is documenting a C script bug), the C DAO script must be patched and the Rust code kept as-is, with a protocol-level constraint added to reject transactions where `header_dep_index > 255` before they reach the C script.

---

### Proof of Concept

The production test `check_dao_withdraw_header_dep_index_exceeds_u8` (`util/dao/src/tests.rs`, lines 475–537) directly demonstrates the discrepancy:

- 258 `header_deps` are constructed; `header_deps[1]` = deposit block (number 100), `header_deps[257]` = withdraw block (number 200).
- Witness `input_type` = `257u64` little-endian.
- Cell data = `100u64` (deposited at block 100).
- C DAO script resolves lowest byte = 1 → deposit block → **ACCEPT**.
- Rust `DaoCalculator` resolves full u64 = 257 → withdraw block (number 200 ≠ 100) → **`DaoError::InvalidOutPoint`**.
- `assert!(result.is_err())` confirms the Rust node rejects what the C script accepts. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L88-99)
```rust
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

**File:** util/dao/src/tests.rs (L489-536)
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

**File:** verification/src/transaction_verifier.rs (L162-171)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
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
