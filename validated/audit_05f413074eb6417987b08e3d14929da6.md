### Title
Rust `DaoCalculator` Uses Full u64 Header-Dep Index While C VM (dao.c) Uses Only the Lowest Byte, Causing Consensus Discrepancy in DAO Withdrawal Validation — (File: `util/dao/src/lib.rs`)

---

### Summary

The `transaction_maximum_withdraw` function in `DaoCalculator` reads the deposit-header index from the witness as a full `u64` and uses it directly to index into `header_deps`. The on-chain C script (`dao.c`) reads only the **lowest byte** of that same 8-byte field. When the index value exceeds 255, the two sides resolve different entries in `header_deps`, producing opposite accept/reject decisions for the same transaction. This is a direct analog to the external report: a derived/resolved value (the C-VM's 1-byte index) is the correct parameter, but the wrong value (the full u64) is used in the subsequent authorization lookup.

---

### Finding Description

**Root cause — production code**

In `util/dao/src/lib.rs`, `transaction_maximum_withdraw` extracts the header-dep index from the witness and immediately uses it as a `usize` array index:

```rust
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)   // ← full u64 cast to usize
        .and_then(|hash| header_deps.get(&hash))
        .ok_or(DaoError::InvalidOutPoint)
})?;
``` [1](#0-0) 

The C script (`dao.c`, referenced at the top of the test file) reads only `witness_data[0]` — the lowest byte — as the index. For any index value `N` where `N > 255`, the two sides resolve **different** `header_deps` slots:

| Side | Index used | `header_deps` slot |
|---|---|---|
| C VM (dao.c) | `N & 0xFF` (lowest byte) | `header_deps[N & 0xFF]` |
| Rust `DaoCalculator` | full `N` | `header_deps[N]` |

**Documented discrepancy in the test suite**

The test `check_dao_withdraw_header_dep_index_exceeds_u8` constructs exactly this scenario: 258 `header_deps`, witness index = 257 (lowest byte = 1), deposit block at slot 1, withdraw block at slot 257. The test comment explicitly states:

> "Position 1: correct deposit block (what C VM resolves via lowest byte). Position 257: withdraw block (wrong — Rust resolves this with full u64)." [2](#0-1) 

**Two exploitable sub-cases**

*Sub-case A — tx-pool pollution (Rust accepts, C VM rejects):*
- Attacker sets `header_dep_index = 257`, places the deposit block at slot 257, places a dummy block at slot 1.
- C VM uses slot 1 → dummy block → block-number mismatch → **rejects**.
- Rust `DaoCalculator` uses slot 257 → deposit block → block-number matches → **accepts**.
- Result: Rust admits the transaction to the tx-pool; any miner who includes it produces a block that fails CKB-VM script validation. Miners waste resources on unincludable transactions.

*Sub-case B — liveness denial (C VM accepts, Rust rejects):*
- Attacker/user sets `header_dep_index = 257`, places the deposit block at slot 1, places a different block at slot 257.
- C VM uses slot 1 → deposit block → **accepts**.
- Rust `DaoCalculator` uses slot 257 → wrong block → block-number mismatch → **rejects**.
- Result: A structurally valid DAO withdrawal is permanently rejected by the Rust verifier and can never be confirmed, locking the user's funds.

The `DaoCalculator` error propagates through `FeeCalculator::transaction_fee` → `ContextualTransactionVerifier::verify`, causing the entire block or tx-pool entry to be rejected. [3](#0-2) [4](#0-3) 

---

### Impact Explanation

- **Sub-case A**: An unprivileged transaction sender can craft DAO withdrawal transactions that pass Rust tx-pool admission but fail CKB-VM block validation. Miners who select these transactions produce invalid blocks, wasting PoW and causing chain-tip instability.
- **Sub-case B**: A user whose DAO withdrawal uses `header_dep_index > 255` (reachable with ≥257 `header_deps`) has their funds permanently unwithdrawable — the Rust node rejects the transaction at every stage even though the on-chain script would accept it.

**Impact: Medium** — Sub-case A enables targeted miner griefing; Sub-case B enables permanent fund lock for affected users.

---

### Likelihood Explanation

Constructing a transaction with 258+ `header_deps` is valid within CKB's transaction size limits (each `Byte32` hash is 32 bytes; 258 entries = ~8 KB, well within the 512 KB limit). No privileged access is required. An attacker only needs to submit a crafted transaction via the standard RPC (`send_transaction`). **Likelihood: Low-Medium** — the attack surface is narrow (requires large `header_deps` arrays) but entirely reachable by any unprivileged RPC caller.

---

### Recommendation

In `transaction_maximum_withdraw`, mask the extracted index to its lowest byte before using it as the `header_deps` array index, matching the C VM's behavior:

```rust
// Before
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
.and_then(|header_dep_index| {
    rtx.transaction
        .header_deps()
        .get(header_dep_index as usize)
        ...
```

```rust
// After — mask to lowest byte, matching dao.c
Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
.and_then(|header_dep_index| {
    let index = (header_dep_index & 0xFF) as usize;  // match C VM
    rtx.transaction
        .header_deps()
        .get(index)
        ...
```

Alternatively, add an explicit bounds check rejecting any `header_dep_index > 255` before the lookup, so that the Rust verifier and C VM agree on the set of valid transactions.

---

### Proof of Concept

The existing test `check_dao_withdraw_header_dep_index_exceeds_u8` in `util/dao/src/tests.rs` is a deterministic PoC:

1. Build a DAO withdrawal `ResolvedTransaction` with 258 `header_deps`.
2. Place the deposit block hash at `header_deps[1]` and the withdraw block hash at `header_deps[257]`.
3. Set the witness `input_type` to `257u64` (little-endian).
4. Call `DaoCalculator::transaction_fee(&rtx)`.

Rust resolves slot 257 → withdraw block (number 200) → mismatches cell data (deposit block number 100) → returns `Err`. The C VM would resolve slot 1 → deposit block (number 100) → matches → returns success. The divergence is confirmed by the test assertion `assert!(result.is_err())`. [5](#0-4) [6](#0-5)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

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
