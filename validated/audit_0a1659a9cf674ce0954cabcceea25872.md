The code evidence is conclusive. Here is the full analysis:

---

### Title
RPC-Triggered Node Panic via `get_block_economic_state` on Freezer-Wiped Block — (`store/src/store.rs`, `util/reward-calculator/src/lib.rs`)

### Summary

`get_cellbase` in `store/src/store.rs` has no freezer fallback, unlike `get_block` and `get_transaction_with_info`. After the freezer wipes a block's body from the kv store, any RPC call to `get_block_economic_state` for that block number reaches `block_reward_internal`, which calls `get_cellbase(...).expect("target cellbase exist")` and panics, crashing the node process.

### Finding Description

**Step 1 — `get_cellbase` has no freezer fallback.**

`get_cellbase` only reads from `COLUMN_BLOCK_BODY` in the kv store: [1](#0-0) 

Compare with `get_block` and `get_transaction_with_info`, which both explicitly check `freezer.number()` and call `freezer.retrieve(...)` before falling back to the kv store: [2](#0-1) [3](#0-2) 

`get_cellbase` has no such guard. It will return `None` for any block whose body has been wiped from the kv store.

**Step 2 — `wipe_out_frozen_data` deletes the cellbase from the kv store.**

After freezing, `wipe_out_frozen_data` calls `batch.delete_block_body(...)` for every frozen block hash, removing all entries (including the cellbase at index 0) from `COLUMN_BLOCK_BODY`: [4](#0-3) 

`compact_block_body` then calls `compact_range` on `COLUMN_BLOCK_BODY` from the start hash to the end hash with `TX_INDEX_UPPER_BOUND` as the upper index bound — this is a RocksDB compaction hint that consolidates the tombstones left by the deletions: [5](#0-4) [6](#0-5) 

After this, `get_cellbase` for any frozen block hash returns `None`.

**Step 3 — `block_reward_internal` panics on `None`.**

`block_reward_internal` calls `get_cellbase` and immediately unwraps with `.expect`: [7](#0-6) 

There is no `?`-propagation or graceful error path — this is a hard `panic!`.

**Step 4 — The RPC entrypoint is `get_block_economic_state`.**

`get_block_economic_state` (in `rpc/src/module/chain.rs`) is a standard Chain RPC method. It calls `block_reward_for_target`, which calls `block_reward_internal`: [8](#0-7) 

No authentication or privilege is required to call this RPC endpoint.

### Impact Explanation

Any caller with RPC access can crash the CKB node process by calling `get_block_economic_state` with a block number that has been frozen and wiped. The panic is unrecoverable — the process terminates. This is a **remote denial-of-service** against any node running with the freezer enabled.

### Likelihood Explanation

The freezer is a supported production feature that activates automatically after epoch 2 (`THRESHOLD_EPOCH = 2`). On mainnet, the vast majority of blocks are frozen. The RPC is typically bound to `127.0.0.1` by default, but any local process (or a remote caller if the operator exposes the RPC port) can trigger this. The trigger is trivially reproducible: call `get_block_economic_state` with any block number below the freezer threshold.

### Recommendation

Fix `get_cellbase` to include the same freezer fallback pattern used by `get_block` and `get_transaction_with_info`: check `freezer.number()`, call `freezer.retrieve(header.number())`, and extract the cellbase (index 0) from the frozen block data. Additionally, replace the `.expect("target cellbase exist")` in `block_reward_internal` with a proper `DaoError` return so that even if the lookup fails, the RPC returns a graceful error instead of panicking.

### Proof of Concept

1. Start a CKB node with the freezer enabled and sync past epoch 2.
2. Wait for the freezer background thread to freeze and wipe blocks (or trigger it manually).
3. Call `get_block_economic_state` via RPC for any block number below the freezer threshold.
4. Observe the node process panic with `"target cellbase exist"` rather than returning a JSON error response.

### Citations

**File:** store/src/store.rs (L42-54)
```rust
    fn get_block(&self, h: &packed::Byte32) -> Option<BlockView> {
        let header = self.get_block_header(h)?;
        if let Some(freezer) = self.freezer()
            && header.number() > 0
            && header.number() < freezer.number()
        {
            let raw_block = freezer.retrieve(header.number()).expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == h.as_slice() {
                return Some(raw_block_reader.to_entity().into_view());
            }
        }
```

**File:** store/src/store.rs (L321-336)
```rust
        if let Some(freezer) = self.freezer()
            && tx_info.block_number > 0
            && tx_info.block_number < freezer.number()
        {
            let raw_block = freezer
                .retrieve(tx_info.block_number)
                .expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == tx_info.block_hash.as_slice()
                && let Some(tx_reader) = raw_block_reader.transactions().get(tx_info.index)
                && tx_reader.calc_tx_hash().as_slice() == hash.as_slice()
            {
                return Some((tx_reader.to_entity().into_view(), tx_info));
            }
        }
```

**File:** store/src/store.rs (L469-477)
```rust
    fn get_cellbase(&self, hash: &packed::Byte32) -> Option<TransactionView> {
        let key = packed::TransactionKey::new_builder()
            .block_hash(hash.to_owned())
            .build();
        self.get(COLUMN_BLOCK_BODY, key.as_slice()).map(|slice| {
            let reader = packed::TransactionViewReader::from_slice_should_be_ok(slice.as_ref());
            Into::<TransactionView>::into(reader)
        })
    }
```

**File:** shared/src/shared.rs (L222-224)
```rust
            for (hash, (number, txs)) in &frozen {
                batch.delete_block_body(*number, hash, *txs).map_err(|e| {
                    ckb_logger::error!("Freezer delete_block_body failed {}", e);
```

**File:** shared/src/shared.rs (L282-299)
```rust
    fn compact_block_body(&self, start: &packed::Byte32, end: &packed::Byte32) {
        let start_t = packed::TransactionKey::new_builder()
            .block_hash(start.clone())
            .index(0u32)
            .build();

        let end_t = packed::TransactionKey::new_builder()
            .block_hash(end.clone())
            .index(TX_INDEX_UPPER_BOUND)
            .build();

        if let Err(e) = self.store.compact_range(
            COLUMN_BLOCK_BODY,
            Some(start_t.as_slice()),
            Some(end_t.as_slice()),
        ) {
            ckb_logger::error!("Freezer compact_range {}-{} error {}", start, end, e);
        }
```

**File:** util/constant/src/store.rs (L1-4)
```rust
/// This value is try to set tx key range as tight as possible,
/// so that db iterating can stop sooner, rather than walking over the whole range of tombstones.
/// empty_tx_size = 72
pub const TX_INDEX_UPPER_BOUND: u32 = 597 * 1000 / 72;
```

**File:** util/reward-calculator/src/lib.rs (L63-78)
```rust
    pub fn block_reward_for_target(
        &self,
        target: &HeaderView,
    ) -> Result<(Script, BlockReward), DaoError> {
        let finalization_parent_number = target
            .number()
            .checked_add(self.consensus.finalization_delay_length())
            .and_then(|number| number.checked_sub(1))
            .ok_or(DaoError::Overflow)?;
        let parent = self
            .store
            .get_block_hash(finalization_parent_number)
            .and_then(|hash| self.store.get_block_header(&hash))
            .expect("block hash checked before involving get_ancestor");
        self.block_reward_internal(target, &parent)
    }
```

**File:** util/reward-calculator/src/lib.rs (L90-101)
```rust
        let target_lock = CellbaseWitness::from_slice(
            &self
                .store
                .get_cellbase(&target.hash())
                .expect("target cellbase exist")
                .witnesses()
                .get(0)
                .expect("target witness exist")
                .raw_data(),
        )
        .expect("cellbase loaded from store should has non-empty witness")
        .lock();
```
