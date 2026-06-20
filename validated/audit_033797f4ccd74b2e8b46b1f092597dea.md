### Title
DAO Withdrawal `header_dep_index` Type Mismatch Between On-Chain `dao.c` (u8 truncation) and Off-Chain `DaoCalculator` (full u64) Causes Valid Transactions to Be Rejected — (File: `util/dao/src/lib.rs`)

---

### Summary

The off-chain Rust `DaoCalculator` reads the `header_dep_index` from the witness `input_type` field as a full `u64`, while the on-chain `dao.c` script reads only the **lowest byte** (effectively treating it as `u8`). When `header_dep_index > 255`, the two components resolve different entries from `header_deps`, creating a semantic split: a DAO withdrawal transaction that is valid on-chain (C VM succeeds) is incorrectly rejected by the Rust node's fee calculator, permanently blocking that transaction from the tx-pool.

---

### Finding Description

In `DaoCalculator::transaction_maximum_withdraw` (`util/dao/src/lib.rs`), the Rust code reads the full 8-byte little-endian `u64` from the witness `input_type` field:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses that value directly as an array index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The on-chain `dao.c` script, however, reads only the **lowest byte** of the same `input_type` field (i.e., `index = input_type[0]`, a `u8`). This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

For `header_dep_index = 257` (stored as 8-byte LE `0x0101000000000000`):

| Component | Reads index | Resolves to | Outcome |
|---|---|---|---|
| On-chain `dao.c` (C VM) | `257 & 0xFF = 1` | `header_deps[1]` = deposit block | **SUCCESS** |
| Off-chain `DaoCalculator` (Rust) | `257` | `header_deps[257]` = wrong block | **ERROR** |

The Rust calculator then checks `deposit_header.number() != deposited_block_number` (e.g., `200 != 100`) and returns `DaoError::InvalidOutPoint`, rejecting the transaction.

The `FeeCalculator` wrapping `DaoCalculator` is called inside `ContextualTransactionVerifier::verify()` via `self.fee_calculator.transaction_fee()?`, meaning any error propagates as a hard rejection from the tx-pool admission path.

---

### Impact Explanation

A transaction sender can craft a valid DAO withdrawal transaction — one the on-chain `dao.c` script would accept — by placing the deposit block hash at `header_deps[index & 0xFF]` and setting `header_dep_index` to any value `> 255` whose lowest byte equals that position. The Rust node's `ContextualTransactionVerifier` will reject this transaction at the fee-calculation step, even though script execution would succeed. The user's DAO funds become unwithdrawable via the normal submission path. The `calculate_dao_maximum_withdraw` RPC also returns incorrect results for such transactions.

---

### Likelihood Explanation

A transaction needs more than 255 `header_deps` entries for the index to exceed `u8` range. While uncommon in practice, there is no protocol-level cap on `header_deps` count below 256, and a user with many DAO cells referencing many distinct blocks could legitimately construct such a transaction. The discrepancy is already documented in the test suite, confirming the scenario is reachable and reproducible.

---

### Recommendation

Align the Rust `DaoCalculator` with the on-chain `dao.c` behavior. Either:

1. **Truncate to u8** in `DaoCalculator::transaction_maximum_withdraw` before indexing: read only the lowest byte of `header_dep_index` (`header_dep_index & 0xFF`) to match the C VM, **or**
2. **Add an explicit validation** that rejects any `header_dep_index > 255` with a clear error before the index is used, so the Rust node and the C VM agree on which transactions are invalid.

Option 2 is safer because it makes the constraint explicit and prevents ambiguous transactions from ever being submitted.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the split: [1](#0-0) 

```
// Pad header_deps to 258 entries so index 257 is valid.
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
let dummy = h256!("0x1").into();
let mut header_deps = vec![dummy; 258];
header_deps[1] = deposit_block.hash();
header_deps[257] = withdraw_block.hash();
``` [2](#0-1) 

The witness stores `input_type = 257` (8-byte LE). The Rust calculator resolves `header_deps[257]` = withdraw block (number 200), then fails the check `deposit_header.number() != deposited_block_number` (200 ≠ 100). The C VM would resolve `header_deps[1]` = deposit block (number 100) and succeed.

The root cause is in `DaoCalculator::transaction_maximum_withdraw`: [3](#0-2) 

The `LittleEndian::read_u64` at line 91 reads the full 8-byte index, while `dao.c` reads only byte 0. Any `header_dep_index` value where `value != (value & 0xFF)` (i.e., `value > 255`) produces divergent behavior between the two components.

### Citations

**File:** util/dao/src/tests.rs (L489-495)
```rust
    // Pad header_deps to 258 entries so index 257 is valid.
    // Position 1: correct deposit block (what C VM resolves via lowest byte).
    // Position 257: withdraw block (wrong — Rust resolves this with full u64).
    let dummy = h256!("0x1").into();
    let mut header_deps = vec![dummy; 258];
    header_deps[1] = deposit_block.hash();
    header_deps[257] = withdraw_block.hash();
```

**File:** util/dao/src/tests.rs (L512-536)
```rust
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
