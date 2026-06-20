### Title
DAO Withdrawal Deposit-Header Index Truncation Mismatch Between Rust `DaoCalculator` and On-Chain C Script — (File: `util/dao/src/lib.rs`)

---

### Summary

The Rust `DaoCalculator` reads the header-dep index stored in a DAO withdrawal witness as a full `u64`, while the on-chain C DAO script reads only the **lowest byte** of that same field. When a transaction sender supplies a witness index value greater than 255 (e.g., 257), the two implementations resolve different entries in `header_deps`. The Rust node therefore computes fee/maximum-withdraw against the wrong block header, causing it to reject transactions that the consensus script would accept — a reachable, sender-controlled transaction-censorship path.

---

### Finding Description

The DAO withdrawal flow requires the transaction witness (`WitnessArgs.input_type`) to carry an index that points to the deposit block's entry in `header_deps`. The Rust `DaoCalculator` (in `util/dao/src/lib.rs`) deserialises this field as a raw `u64` and uses it directly to index `transaction.header_deps()`.

The on-chain C DAO script, however, reads the same field using a single-byte load, effectively masking the value to its lowest 8 bits. For any index value whose lowest byte differs from the full value — i.e., any value `> 255` — the two sides resolve **different** headers:

| Witness value | Rust resolves | C script resolves |
|---|---|---|
| 257 (0x101) | `header_deps[257]` | `header_deps[1]` |

The test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` explicitly documents this split:

```
// Rust resolves index 257 → withdraw block (number 200), but cell data
// says deposited at block 100. Block number check catches the mismatch.
assert!(result.is_err(), "expected Err, got {result:?}");
``` [1](#0-0) 

The test constructs a `header_deps` vector of 258 entries, places the deposit block at index 1 and the withdraw block at index 257, then sets the witness index to `257u64`. The Rust code resolves index 257 (withdraw block), finds a block-number mismatch with the cell data, and returns an error. The C script would resolve index 1 (deposit block), find a match, and **accept** the transaction.

The `DaoCalculator` is invoked during tx-pool admission (`transaction_fee`) and block-assembly verification. Its rejection of a transaction that the consensus script would validate as valid constitutes a node-level censorship of otherwise-valid DAO withdrawals. [2](#0-1) 

---

### Impact Explanation

A transaction sender who crafts a DAO withdrawal with a witness index `> 255` (where the lowest byte still points to the correct deposit header) will have their transaction rejected by every honest Rust node's tx-pool. The transaction is valid per consensus (the C script accepts it), but no Rust node will relay or mine it. This is a **targeted, sender-controlled transaction-censorship** vulnerability: the sender cannot recover their DAO-locked CKBytes through the normal relay path.

---

### Likelihood Explanation

The attack surface is reachable by any unprivileged transaction sender. Constructing a `header_deps` list with more than 256 entries and placing the deposit header at a position whose index has a non-zero high byte is straightforward. The discrepancy is deterministic and reproducible. Likelihood is **medium**: it requires a specific witness encoding that most wallets would never produce, but a motivated actor can craft it deliberately.

---

### Recommendation

In `util/dao/src/lib.rs`, when reading the header-dep index from `WitnessArgs.input_type`, truncate the parsed `u64` to its lowest byte (cast to `u8` then to `usize`) before indexing into `header_deps`, matching the behaviour of the on-chain C script. Alternatively, add an explicit validation step that rejects any witness index value `> 255` with a clear error, so the Rust node and the C script agree on the set of valid transactions.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` directly demonstrates the split:

1. `header_deps[1]` = deposit block (number 100); `header_deps[257]` = withdraw block (number 200).
2. Witness `input_type` = `257u64` (little-endian bytes).
3. C script reads lowest byte → index 1 → deposit block → **valid**.
4. Rust `DaoCalculator` reads full u64 → index 257 → withdraw block → block-number mismatch → **rejected**. [3](#0-2) 

The root cause assignment is in `util/dao/src/lib.rs` where the witness index is consumed as a raw `u64` without truncation to match the C script's single-byte read semantics. [4](#0-3)

### Citations

**File:** util/dao/src/tests.rs (L476-536)
```rust
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
```

**File:** util/dao/src/lib.rs (L303-334)
```rust
    fn input_occupied_capacities(&self, rtx: &ResolvedTransaction) -> CapacityResult<Capacity> {
        rtx.resolved_inputs
            .iter()
            .try_fold(Capacity::zero(), |capacities, cell_meta| {
                let current_capacity = modified_occupied_capacity(cell_meta, self.consensus);
                current_capacity.and_then(|c| capacities.safe_add(c))
            })
    }

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
}
```
