### Title
DAO Withdrawal Header-Dep Index Resolution Mismatch Between Rust `DaoCalculator` and On-Chain `dao.c` Script — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator::transaction_maximum_withdraw()` reads the **full `u64`** value from `WitnessArgs.input_type` as the `header_deps` index, while the on-chain `dao.c` script reads only the **lowest byte (u8)** of the same 8-byte field. This is a direct analog to the reported fee-target mismatch: two code paths that are supposed to resolve the same entity (the deposit header) use different "targets" — one uses the full index, the other uses only its lowest byte. A transaction author can craft a DAO withdrawal where the C VM script approves the transaction (using the correct deposit header at `header_deps[lowest_byte(index)]`) while the Rust node's fee verifier rejects it (using a different header at `header_deps[full_u64(index)]`), or vice versa.

---

### Finding Description

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw()` extracts the deposit header index from the witness as a full `u64`:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
```

and then indexes `header_deps` with it:

```rust
rtx.transaction
    .header_deps()
    .get(header_dep_index as usize)
``` [1](#0-0) 

The on-chain `dao.c` script, however, reads only the **lowest byte** of the same 8-byte `input_type` field as the index into `header_deps`. This is explicitly documented in the test `check_dao_withdraw_header_dep_index_exceeds_u8`:

> "Position 1: correct deposit block (what C VM resolves via lowest byte)."
> "Position 257: withdraw block (wrong — Rust resolves this with full u64)."
> "Rust resolves index 257 → withdraw block (number 200), but cell data says deposited at block 100. Block number check catches the mismatch." [2](#0-1) 

The `DaoCalculator` is invoked both during **tx-pool admission** (`tx-pool/src/util.rs:check_tx_fee`) and during **block validation** (`verification/src/transaction_verifier.rs:FeeCalculator::transaction_fee`): [3](#0-2) [4](#0-3) 

The concrete mismatch scenario:

| Step | C VM (`dao.c`) | Rust `DaoCalculator` |
|---|---|---|
| Witness `input_type` | `257` (8 bytes LE) | `257` (8 bytes LE) |
| Index used | `257 & 0xFF = 1` | `257` (full u64) |
| Header resolved | `header_deps[1]` = deposit block ✓ | `header_deps[257]` = wrong block ✗ |
| Block number check | passes | fails → `DaoError::InvalidOutPoint` |

A transaction author who places the correct deposit block at `header_deps[1]` and any other block at `header_deps[257]` will have a transaction that `dao.c` approves but the Rust node rejects.

---

### Impact Explanation

**High** — Two concrete impacts:

1. **Permanent liveness failure for valid DAO withdrawals**: Any DAO withdrawal transaction where the witness `input_type` index exceeds 255 (i.e., `header_deps` has > 255 entries and the deposit header is at a position whose full u64 value differs from its lowest byte) is permanently rejected by the Rust node's fee verifier and block validator, even though the on-chain `dao.c` script would approve it. The user's DAO funds become unwithdrawable through the standard path.

2. **Consensus-layer rejection of valid blocks**: If a miner directly assembles a block containing such a transaction (bypassing the tx-pool), the block passes `dao.c` script execution but is rejected by the Rust `FeeCalculator` during contextual block verification. All Rust nodes reject the block, causing the miner to lose the block reward. Since the C VM script is the authoritative authorization layer, the Rust node is incorrectly rejecting a protocol-valid transaction.

---

### Likelihood Explanation

**Medium** — The attacker must be a DAO depositor who crafts a withdrawal transaction with more than 255 `header_deps` entries and places the deposit header at a position whose index exceeds 255. This is an unusual but valid transaction structure. No privileged access, key compromise, or majority hashpower is required. The entry path is the standard DAO withdrawal RPC/tx submission flow, reachable by any unprivileged transaction sender.

---

### Recommendation

Align the Rust `DaoCalculator` to read only the lowest byte (u8) of the `input_type` field as the `header_deps` index, matching the behavior of the on-chain `dao.c` script:

```rust
// Current (reads full u64):
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))

// Fixed (reads only lowest byte, matching dao.c):
Ok(u64::from(header_deps_index_data.unwrap()[0]))
``` [1](#0-0) 

Alternatively, enforce that the `input_type` index must fit in a `u8` and reject transactions where it does not, making the constraint explicit and consistent.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the inconsistency:

- 258 `header_deps` are constructed; `header_deps[1]` = deposit block (block 100), `header_deps[257]` = withdraw block (block 200).
- Witness `input_type` = `257u64` (little-endian 8 bytes).
- C VM (`dao.c`) reads lowest byte = `1` → resolves `header_deps[1]` = deposit block → **approves**.
- Rust `DaoCalculator` reads full u64 = `257` → resolves `header_deps[257]` = withdraw block → block number 200 ≠ cell data 100 → **rejects with `DaoError::InvalidOutPoint`**.

The test asserts `result.is_err()`, confirming the Rust node rejects a transaction the C VM script would accept. [5](#0-4)

### Citations

**File:** util/dao/src/lib.rs (L91-96)
```rust
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
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
