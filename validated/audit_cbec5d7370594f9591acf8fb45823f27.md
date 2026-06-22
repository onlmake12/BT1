### Title
DAO Withdrawal Permanently Rejected by Tx-Pool Due to Header-Dep Index Width Mismatch in `DaoCalculator` - (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::transaction_maximum_withdraw()` reads the header-dep index from the witness as a full `u64`, while the on-chain C DAO script interprets only the lowest byte of that same field. When a DAO phase-2 withdrawal transaction encodes an index value whose full `u64` resolves to a different (wrong) header than its lowest byte, the Rust fee-check in the tx-pool returns `DaoError::InvalidOutPoint`, the transaction is rejected as `Reject::Malformed`, and any remote peer that relayed it is permanently banned — even though the transaction would pass CKB-VM script execution.

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw()` extracts the deposit-block header-dep index from the witness `input_type` field as a full 8-byte little-endian `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

It then uses that value directly as a `usize` array index into `header_deps`:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
    .and_then(|hash| header_deps.get(&hash))
    .ok_or(DaoError::InvalidOutPoint)
```

The C DAO script (referenced at `dao_user.rs` line 14 as `ckb-system-scripts/c/dao.c#L81`) reads only the **lowest byte** of the same 8-byte field. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs`:

```
// Position 1: correct deposit block (what C VM resolves via lowest byte).
// Position 257: withdraw block (wrong — Rust resolves this with full u64).
```

When a transaction carries `input_type = 257` (0x0101 LE), the C VM resolves `header_deps[1]` (the correct deposit block), while the Rust calculator resolves `header_deps[257]` (a different block). The subsequent block-number cross-check at line 105 then fails:

```rust
if deposit_header.number() != deposited_block_number {
    return Err(DaoError::InvalidOutPoint);
}
```

This error propagates through `check_tx_fee()` in `tx-pool/src/util.rs`:

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

`Reject::Malformed` is classified as `is_malformed_tx() = true` in `util/types/src/core/tx_pool.rs`:

```rust
Reject::Malformed(_, _) => true,
```

In `tx-pool/src/process.rs`, any remote peer that relayed such a transaction is then permanently banned:

```rust
if reject.is_malformed_tx() {
    self.ban_malformed(peer, format!("reject {reject}")).await;
}
```

The same `DaoCalculator::transaction_fee()` is also invoked by `FeeCalculator::transaction_fee()` inside `ContextualTransactionVerifier` in `verification/src/transaction_verifier.rs`:

```rust
DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
    .transaction_fee(&self.transaction)
```

This means the mismatch also affects block-level verification: a block containing such a transaction would be rejected by nodes running this code, even though CKB-VM script execution would succeed.

### Impact Explanation

A DAO depositor who constructs a phase-2 withdrawal transaction with ≥ 256 `header_deps` entries and places the deposit-block hash at a position whose index value exceeds 255 (e.g., index 257) will have their transaction permanently rejected by every node's tx-pool with `PoolRejectedMalformedTransaction`. The user receives no actionable error message explaining the index-width mismatch. Any peer that relayed the transaction is banned. The deposited CKB remains locked in the DAO cell until the user discovers the workaround (reordering `header_deps` so the deposit block falls at index < 256). If the transaction was included in a block by a miner who bypassed the tx-pool, other nodes would reject the block.

### Likelihood Explanation

The trigger condition — a DAO withdrawal with more than 255 distinct `header_deps` — arises when a single transaction simultaneously withdraws DAO cells deposited across more than 255 different blocks. Wallets or scripts that batch-withdraw large numbers of DAO positions can reach this threshold. The condition is not adversarially constructed; it is a natural consequence of the protocol allowing arbitrary numbers of `header_deps`. The discrepancy is already captured in a production test, confirming the code path is reachable.

### Recommendation

In `DaoCalculator::transaction_maximum_withdraw()` (`util/dao/src/lib.rs`, line 91), mask the parsed index to its lowest byte before using it as an array index, to match the C DAO script's behavior:

```rust
let header_dep_index = LittleEndian::read_u64(&header_deps_index_data.unwrap()) & 0xFF;
```

Alternatively, add an explicit bounds check that rejects indices ≥ 256 with a clear `DaoError` variant, and update the DAO script to enforce the same limit, so both layers agree on what constitutes a valid transaction.

### Proof of Concept

The existing unit test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the root cause:

1. Build a DAO withdrawal `ResolvedTransaction` with 258 `header_deps`.
2. Place the correct deposit block at `header_deps[1]` and the withdraw block at `header_deps[257]`.
3. Set `witness.input_type = 257u64` (lowest byte = 1).
4. Call `DaoCalculator::transaction_fee(&rtx)`.

The Rust calculator resolves index 257 → withdraw block (number 200), then checks `deposit_header.number() != deposited_block_number` (200 ≠ 100) and returns `Err(DaoError::InvalidOutPoint)`. The C DAO script would resolve index 1 → deposit block (number 100), pass the check, and accept the transaction.

In a live node, submitting this transaction via `send_transaction` RPC triggers `check_tx_fee` → `DaoCalculator::transaction_fee` → `Reject::Malformed("InvalidOutPoint", ...)`, returning RPC error `-1108 PoolRejectedMalformedTransaction`. If relayed by a peer, that peer is banned via `ban_malformed`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** tx-pool/src/util.rs (L34-41)
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

**File:** util/types/src/core/tx_pool.rs (L89-96)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
```

**File:** tx-pool/src/process.rs (L514-515)
```rust
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
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
