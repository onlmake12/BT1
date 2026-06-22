### Title
`CompactBlockProcess::execute` Skips `BLOCK_INVALID` Status Update After `ReconstructionResult::Error(CompactBlockHasInvalidUncle)` - (File: sync/src/relayer/compact_block_process.rs)

---

### Summary

When `reconstruct_block` returns `ReconstructionResult::Error(CompactBlockHasInvalidUncle)`, the block hash is never written to `block_status_map` as `BLOCK_INVALID`. Because `contextual_check` gates on that flag to short-circuit re-processing, any peer can repeatedly relay the same compact block and force the node to re-execute the full reconstruction pipeline each time.

---

### Finding Description

`CompactBlockProcess::execute` in `sync/src/relayer/compact_block_process.rs` follows this sequence:

1. `non_contextual_check` — structural size/staleness checks  
2. `contextual_check` — checks `block_status_map` for `BLOCK_STORED`, `BLOCK_RECEIVED`, or **`BLOCK_INVALID`**; if none match, continues  
3. `CompactBlockVerifier::verify` — internal compact-block consistency  
4. `shared.insert_valid_header(self.peer, &header)` — **writes the header into the header map as `HEADER_VALID`**  
5. `reconstruct_block` — assembles the full block  
6. Matches on `ReconstructionResult` [1](#0-0) 

The `ReconstructionResult::Error` arm at line 172 simply returns the status:

```rust
ReconstructionResult::Error(status) => status,
```

No call to `shared.shared().insert_block_status(block_hash, BlockStatus::BLOCK_INVALID)` is made. [2](#0-1) 

`CompactBlockHasInvalidUncle` is produced inside `reconstruct_block` when an uncle's status is already `BLOCK_INVALID`:

```rust
BlockStatus::BLOCK_INVALID => {
    return ReconstructionResult::Error(
        StatusCode::CompactBlockHasInvalidUncle.with_context(uncle_hash),
    );
}
``` [3](#0-2) 

This is a deterministic, permanent failure: once an uncle is `BLOCK_INVALID`, it stays that way. The block referencing it is therefore permanently invalid. Yet the block hash is never written as `BLOCK_INVALID`.

Contrast this with the header-verification path inside `contextual_check`, which **does** set the flag on failure:

```rust
shared.shared().insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
return StatusCode::CompactBlockHasInvalidHeader.with_context(format!("{block_hash} {err}"));
``` [4](#0-3) 

The early-exit guard in `contextual_check` that would cheaply reject re-submissions is:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [5](#0-4) 

Because `BLOCK_INVALID` is never set, this guard is never reached for the affected block. Every subsequent relay of the same compact block re-enters the full pipeline: `non_contextual_check` → `contextual_check` → `CompactBlockVerifier::verify` → `insert_valid_header` → `reconstruct_block`.

The `BlockStatus` flag hierarchy is defined as: [6](#0-5) 

---

### Impact Explanation

Any unprivileged relay peer can send the same compact block (whose uncle is locally `BLOCK_INVALID`) repeatedly. Each delivery forces the node through the full reconstruction pipeline — including a tx-pool fetch (`fetch_txs`) and uncle-status lookups — before failing at the same point. Because the `BLOCK_INVALID` flag is never written, the cheap early-exit in `contextual_check` is permanently bypassed. This is a targeted CPU/memory exhaustion vector against any CKB full node reachable over the P2P relay protocol.

**Impact: Medium**

---

### Likelihood Explanation

The precondition — a locally `BLOCK_INVALID` uncle — arises naturally during normal chain operation whenever a peer relays a block that fails contextual verification (wrong PoW, bad epoch, invalid transactions). An attacker who observes such a rejection can immediately craft a compact block referencing that uncle hash and relay it to the victim. No

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L98-173)
```rust
        match ret {
            ReconstructionResult::Block(block) => {
                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_transaction_count
                        .inc_by(block.transactions().len() as u64);
                    metrics.ckb_relay_cb_reconstruct_ok.inc();
                }
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
                // remove all pending request below this block epoch
                //
                // use epoch as the judgment condition because we accept
                // all block in current epoch as uncle block
                pending_compact_blocks.retain(|_, (v, _, _)| {
                    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
                        .number()
                        >= block.epoch().number()
                });
                shrink_to_fit!(pending_compact_blocks, 20);
                self.relayer
                    .accept_block(Arc::clone(&self.nc), self.peer, block, "CompactBlock");

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_verify_duration
                        .observe(instant.elapsed().as_secs_f64());
                }
                Status::ok()
            }
            ReconstructionResult::Missing(transactions, uncles) => {
                let missing_transactions: Vec<u32> =
                    transactions.into_iter().map(|i| i as u32).collect();

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_fresh_tx_cnt
                        .inc_by(missing_transactions.len() as u64);
                    metrics.ckb_relay_cb_reconstruct_fail.inc();
                }

                let missing_uncles: Vec<u32> = uncles.into_iter().map(|i| i as u32).collect();
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;

                StatusCode::CompactBlockRequiresFreshTransactions.with_context(&block_hash)
            }
            ReconstructionResult::Collided => {
                let missing_transactions: Vec<u32> = compact_block
                    .short_id_indexes()
                    .into_iter()
                    .map(|i| i as u32)
                    .collect();
                let missing_uncles: Vec<u32> = vec![];
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;
                StatusCode::CompactBlockMeetsShortIdsCollision.with_context(&block_hash)
            }
            ReconstructionResult::Error(status) => status,
        }
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L333-338)
```rust
            shared
                .shared()
                .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
            return StatusCode::CompactBlockHasInvalidHeader
                .with_context(format!("{block_hash} {err}"));
        }
```

**File:** sync/src/relayer/mod.rs (L468-472)
```rust
                BlockStatus::BLOCK_INVALID => {
                    return ReconstructionResult::Error(
                        StatusCode::CompactBlockHasInvalidUncle.with_context(uncle_hash),
                    );
                }
```

**File:** shared/src/block_status.rs (L1-18)
```rust
//! Provide BlockStatus
#![allow(missing_docs)]
#![allow(clippy::bad_bit_mask)]

use bitflags::bitflags;
bitflags! {
    #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
}
```
