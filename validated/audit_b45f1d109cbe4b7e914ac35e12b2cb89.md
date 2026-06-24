Audit Report

## Title
RPC-Triggered Node Panic via `get_block_economic_state` on Frozen Block — (`store/src/store.rs`, `util/reward-calculator/src/lib.rs`)

## Summary

`get_cellbase` in `store/src/store.rs` reads only from `COLUMN_BLOCK_BODY` in the kv store and has no freezer fallback, unlike `get_block` and `get_transaction_with_info`. After the freezer wipes a block's body, `get_cellbase` returns `None` for that block. `block_reward_internal` in `util/reward-calculator/src/lib.rs` calls `get_cellbase(...).expect("target cellbase exist")`, which panics unconditionally on `None`. Any caller with access to the RPC can trigger this by calling `get_block_economic_state` for any block number below the freezer threshold.

## Finding Description

**Root cause — `get_cellbase` has no freezer fallback.**

`get_cellbase` at [1](#0-0)  reads only from `COLUMN_BLOCK_BODY` in the kv store. It has no check against `freezer.number()` and no call to `freezer.retrieve(...)`.

By contrast, `get_block` at [2](#0-1)  and `get_transaction_with_info` at [3](#0-2)  both explicitly check `freezer.number()` and retrieve the block from the freezer before falling back to the kv store.

**Freezer wipes the cellbase from the kv store.**

`wipe_out_frozen_data` calls `batch.delete_block_body(...)` for every frozen block hash at [4](#0-3) , removing all entries including the cellbase (index 0) from `COLUMN_BLOCK_BODY`. `compact_block_body` then calls `compact_range` on `COLUMN_BLOCK_BODY` at [5](#0-4) , consolidating the tombstones. After this, `get_cellbase` for any frozen block hash returns `None`.

**Hard panic in `block_reward_internal`.**

`block_reward_internal` calls `get_cellbase` and immediately unwraps with `.expect("target cellbase exist")` at [6](#0-5) . There is no `?`-propagation or graceful error path.

**RPC entrypoint.**

`block_reward_for_target` at [7](#0-6)  calls `block_reward_internal`, and is itself called by the `get_block_economic_state` RPC method in `rpc/src/module/chain.rs`. No authentication is required. [8](#0-7) 

## Impact Explanation

Any caller with access to the RPC endpoint can trigger a panic in the RPC handler by calling `get_block_economic_state` for any block number below the freezer threshold. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The RPC is bound to `127.0.0.1` by default, so the trigger is local. If the operator exposes the RPC port externally, the impact escalates to a remotely triggerable node crash, matching **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The freezer activates automatically after epoch 2 on mainnet, meaning the vast majority of blocks on a synced node are frozen and their bodies wiped from the kv store. The trigger requires no special privileges — only the ability to call the standard `get_block_economic_state` RPC with any frozen block number. The condition is permanently satisfied on any fully-synced mainnet node with the freezer enabled.

## Recommendation

Fix `get_cellbase` to include the same freezer fallback pattern used by `get_block` and `get_transaction_with_info`: check `freezer.number()`, call `freezer.retrieve(header.number())`, and extract the transaction at index 0 from the frozen block data. Additionally, replace the `.expect("target cellbase exist")` in `block_reward_internal` with a proper `DaoError` return so that even if the lookup fails, the RPC returns a graceful JSON error instead of panicking.

## Proof of Concept

1. Start a CKB mainnet node with the freezer enabled (default configuration).
2. Sync past epoch 2 and wait for the freezer background thread to freeze and wipe blocks.
3. Identify any block number below the current freezer threshold (e.g., block 1).
4. Call `get_block_economic_state` via RPC: `curl -X POST -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"get_block_economic_state","params":["0x1"],"id":1}' http://127.0.0.1:8114`.
5. Observe the node panic with `"target cellbase exist"` rather than returning a JSON error response.

### Citations

**File:** store/src/store.rs (L44-54)
```rust
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

**File:** shared/src/shared.rs (L222-226)
```rust
            for (hash, (number, txs)) in &frozen {
                batch.delete_block_body(*number, hash, *txs).map_err(|e| {
                    ckb_logger::error!("Freezer delete_block_body failed {}", e);
                    e
                })?;
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

**File:** util/reward-calculator/src/lib.rs (L90-94)
```rust
        let target_lock = CellbaseWitness::from_slice(
            &self
                .store
                .get_cellbase(&target.hash())
                .expect("target cellbase exist")
```

**File:** rpc/src/module/chain.rs (L1-13)
```rust
use crate::error::RPCError;
use crate::util::FeeRateCollector;
use async_trait::async_trait;
use ckb_jsonrpc_types::{
    BlockEconomicState, BlockFilter, BlockNumber, BlockResponse, BlockView, CellWithStatus,
    Consensus, EpochNumber, EpochView, EstimateCycles, FeeRateStatistics, HeaderView, OutPoint,
    ResponseFormat, ResponseFormatInnerType, Timestamp, Transaction, TransactionAndWitnessProof,
    TransactionProof, TransactionWithStatusResponse, Uint32, Uint64,
};
use ckb_logger::error;
use ckb_reward_calculator::RewardCalculator;
use ckb_shared::{Snapshot, shared::Shared};
use ckb_store::{ChainStore, data_loader_wrapper::AsDataLoader};
```
