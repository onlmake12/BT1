Audit Report

## Title
Tampered CompactBlockV1 Extension Permanently Marks Valid Block Hash as BLOCK_INVALID — (`sync/src/relayer/mod.rs`)

## Summary
`reconstruct_block` copies the compact block's extension field verbatim into the reconstructed `BlockV1` with no integrity check against the `extra_hash` committed in the header. When the tampered block is submitted to the chain pipeline, `BlockExtensionVerifier` fails with `InvalidExtraHash`, causing `ConsumeUnverifiedBlockProcessor` to permanently write `BLOCK_INVALID` for the block hash. Because the block hash is derived solely from the header (not the extension), the same hash applies to the legitimate block, which is then permanently rejected for the lifetime of the process.

## Finding Description

**Root cause**: `reconstruct_block` in `sync/src/relayer/mod.rs` at lines 482–490 builds a `BlockV1` using the compact block's extension without verifying it against the header's committed `extra_hash`:

```rust
let block = if let Some(extension) = compact_block.extension() {
    packed::BlockV1::new_builder()
        .header(compact_block.header())   // valid header, valid extra_hash
        .extension(extension)             // ← TAMPERED bytes, unchecked
        .build()
        .as_v0()
``` [1](#0-0) 

The only post-reconstruction integrity check is `transactions_root` — no extension-vs-`extra_hash` check exists at this stage. [2](#0-1) 

**Pre-reconstruction checks are all bypassed**:
- `non_contextual_check` only validates uncle count, proposal count, and block height. [3](#0-2) 
- `contextual_check` runs `HeaderVerifier` against the untampered header — passes. [4](#0-3) 
- `CompactBlockVerifier::verify` only checks prefilled-tx structure and short-id duplicates.

**Tampered block is submitted to chain pipeline**: `ReconstructionResult::Block` triggers `accept_block` at line 119, submitting the tampered block for full verification. [5](#0-4) 

**Verification failure writes BLOCK_INVALID**: `BlockExtensionVerifier` computes `actual_extra_hash` from the tampered extension and compares it to the header-committed value: [6](#0-5) 

On this error, `ConsumeUnverifiedBlockProcessor` calls `insert_block_status` with `BLOCK_INVALID`: [7](#0-6) 

`is_internal_db_error` returns `false` for `BlockErrorKind::InvalidExtraHash` (it only exempts `InternalErrorKind::Database` / `InternalErrorKind::System`), so `BLOCK_INVALID` is unconditionally written: [8](#0-7) 

**Permanent suppression**: `block_status_map` is an in-memory `DashMap`. The entry persists for the process lifetime. [9](#0-8) 

When the legitimate block arrives, `contextual_check` immediately rejects it: [10](#0-9) 

## Impact Explanation

This is a **consensus deviation** vulnerability (Critical, 15001–25000 points). An attacker positioned as a relay peer can permanently suppress a valid mined block on victim nodes by sending a single mutated compact block message. The miner's block is orphaned on all affected nodes. If the attacker peers with a significant fraction of the network, the suppressed block never achieves sufficient propagation to be included in the canonical chain, directly causing consensus deviation and mining revenue loss.

## Likelihood Explanation

- Requires only a standard P2P relay connection — no keys, no hashpower, no Sybil attack.
- The attacker must receive the compact block before the victim, which is trivially achievable for any relay peer (compact blocks are broadcast to all peers).
- The mutation is a single-bit flip in the extension bytes.
- The attack is low-cost and repeatable: the attacker peer is punished/banned after the callback fires, but the `BLOCK_INVALID` entry is already written and the damage is done. A new connection suffices for each attack.

## Recommendation

Before returning `ReconstructionResult::Block`, verify the extension against the header's committed `extra_hash`. A mismatch must **not** set `BLOCK_INVALID` — only the compact block message is malformed, not the block itself. Return `ReconstructionResult::Error` instead, which does not call `accept_block` and therefore never writes `BLOCK_INVALID`:

```rust
// In reconstruct_block, after building the block view, before returning ReconstructionResult::Block:
if let Some(_) = compact_block.extension() {
    let actual_extra_hash = block.calc_extra_hash().extra_hash();
    if actual_extra_hash != block.extra_hash() {
        return ReconstructionResult::Error(
            StatusCode::ProtocolMessageIsMalformed
                .with_context("compact block extension does not match header extra_hash")
        );
    }
}
```

The `ReconstructionResult::Error` path returns a status code without submitting the block to the chain pipeline, so `BLOCK_INVALID` is never written. The peer should be penalized for sending a malformed message. [11](#0-10) 

## Proof of Concept

1. Observe a valid mined `BlockV1` with hash `H` being relayed as a `CompactBlockV1`.
2. As a relay peer of the victim node, receive the compact block.
3. Flip one bit in the `extension` field of the `CompactBlockV1` (leave the header untouched).
4. Forward the mutated compact block to the victim node.
5. Observe that the victim node logs `InvalidExtraHash` and `insert_block_status` is called with `BLOCK_INVALID` for hash `H`.
6. Send the legitimate `BlockV1` (or the original compact block) to the victim node.
7. Observe that the victim node rejects it with `BlockIsInvalid` and never adds it to its chain.
8. Confirm the node must be restarted to clear the in-memory `block_status_map` entry.

### Citations

**File:** sync/src/relayer/mod.rs (L482-490)
```rust
            let block = if let Some(extension) = compact_block.extension() {
                packed::BlockV1::new_builder()
                    .header(compact_block.header())
                    .uncles(uncles)
                    .transactions(txs.into_iter().map(|tx| tx.data()).collect::<Vec<_>>())
                    .proposals(compact_block.proposals())
                    .extension(extension)
                    .build()
                    .as_v0()
```

**File:** sync/src/relayer/mod.rs (L507-527)
```rust
            let compact_block_tx_root = compact_block.header().raw().transactions_root();
            let reconstruct_block_tx_root = block.transactions_root();
            if compact_block_tx_root != reconstruct_block_tx_root {
                if compact_block.short_ids().is_empty()
                    || compact_block.short_ids().len() == block_txs_len
                {
                    return ReconstructionResult::Error(
                        StatusCode::CompactBlockHasUnmatchedTransactionRootWithReconstructedBlock
                            .with_context(format!(
                                "Compact_block_tx_root({}) != reconstruct_block_tx_root({})",
                                compact_block.header().raw().transactions_root(),
                                block.transactions_root(),
                            )),
                    );
                } else {
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_relay_transaction_short_id_collide.inc();
                    }
                    return ReconstructionResult::Collided;
                }
            }
```

**File:** sync/src/relayer/compact_block_process.rs (L99-119)
```rust
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
```

**File:** sync/src/relayer/compact_block_process.rs (L172-172)
```rust
            ReconstructionResult::Error(status) => status,
```

**File:** sync/src/relayer/compact_block_process.rs (L190-222)
```rust
fn non_contextual_check(
    compact_block: &CompactBlock,
    header: &HeaderView,
    consensus: &Consensus,
    active_chain: &ActiveChain,
) -> Status {
    if compact_block.uncles().len() > consensus.max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock uncles count({}) > consensus max_uncles_num({})",
            compact_block.uncles().len(),
            consensus.max_uncles_num()
        ));
    }
    if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock proposals count({}) > consensus max_block_proposals_limit({})",
            compact_block.proposals().len(),
            consensus.max_block_proposals_limit(),
        ));
    }

    // Only accept blocks with a height greater than tip - N
    // where N is the current epoch length
    let block_hash = header.hash();
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }

    Status::ok()
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L293-341)
```rust
    // compact header verification
    let fn_get_pending_header = {
        |block_hash| {
            pending_compact_blocks
                .get(&block_hash)
                .map(|(compact_block, _, _)| {
                    let header = compact_block.header().into_view();
                    HeaderFields {
                        hash: header.hash(),
                        number: header.number(),
                        epoch: header.epoch(),
                        timestamp: header.timestamp(),
                        parent_hash: header.parent_hash(),
                    }
                })
                .or_else(|| {
                    shared
                        .get_header_index_view(&block_hash, false)
                        .map(|header| HeaderFields {
                            hash: header.hash(),
                            number: header.number(),
                            epoch: header.epoch(),
                            timestamp: header.timestamp(),
                            parent_hash: header.parent_hash(),
                        })
                })
        }
    };
    let median_time_context = CompactBlockMedianTimeView {
        fn_get_pending_header: Box::new(fn_get_pending_header),
    };
    let header_verifier = HeaderVerifier::new(&median_time_context, shared.consensus());
    if let Err(err) = header_verifier.verify(compact_block_header) {
        if err
            .downcast_ref::<HeaderError>()
            .map(|e| e.is_too_new())
            .unwrap_or(false)
        {
            return Status::ignored();
        } else {
            shared
                .shared()
                .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
            return StatusCode::CompactBlockHasInvalidHeader
                .with_context(format!("{block_hash} {err}"));
        }
    }

    Status::ok()
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L584-587)
```rust
        let actual_extra_hash = block.calc_extra_hash().extra_hash();
        if actual_extra_hash != block.extra_hash() {
            return Err(BlockErrorKind::InvalidExtraHash.into());
        }
```

**File:** chain/src/verify.rs (L175-177)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
```

**File:** error/src/lib.rs (L101-114)
```rust
pub fn is_internal_db_error(error: &Error) -> bool {
    if error.kind() == ErrorKind::Internal {
        let error_kind = error
            .downcast_ref::<InternalError>()
            .expect("error kind checked")
            .kind();
        if error_kind == InternalErrorKind::DataCorrupted {
            panic!("{}", error)
        } else {
            return error_kind == InternalErrorKind::Database
                || error_kind == InternalErrorKind::System;
        }
    }
    false
```

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
    }
```
