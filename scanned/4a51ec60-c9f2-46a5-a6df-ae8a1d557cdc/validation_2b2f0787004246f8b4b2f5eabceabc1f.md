Audit Report

## Title
Tampered CompactBlock Extension Causes Permanent BLOCK_INVALID Marking for Valid Block Hash — (`sync/src/relayer/mod.rs`, `chain/src/verify.rs`, `verification/contextual/src/contextual_block_verifier.rs`)

## Summary
An unprivileged relay peer can send a `CompactBlockV1` with a valid PoW header but mutated extension bytes. The node reconstructs a full block using the tampered extension, submits it to the chain pipeline, and `BlockExtensionVerifier` fails with `InvalidExtraHash` because the tampered extension does not match the `extra_hash` committed in the header. `ConsumeUnverifiedBlockProcessor` then permanently writes `BLOCK_INVALID` for the block hash. Because the block hash is derived solely from the header (not the extension), the same hash is used for the legitimate block, which is subsequently and permanently rejected by the victim node for the lifetime of the process.

## Finding Description

**Root cause:** `reconstruct_block` in `sync/src/relayer/mod.rs` copies the extension field from the compact block verbatim into the reconstructed `BlockV1` with no integrity check against the `extra_hash` committed in the header:

```rust
let block = if let Some(extension) = compact_block.extension() {
    packed::BlockV1::new_builder()
        .header(compact_block.header())   // original header, valid extra_hash
        .extension(extension)             // ← TAMPERED bytes, unchecked
        .build()
        .as_v0()
``` [1](#0-0) 

The only post-reconstruction check is `transactions_root` — there is no extension-vs-`extra_hash` check: [2](#0-1) 

**Pre-reconstruction checks are all insufficient:**

- `non_contextual_check` only validates uncle count, proposal count, and block height — no extension check: [3](#0-2) 

- `CompactBlockVerifier::verify` only calls `PrefilledVerifier` and `ShortIdsVerifier` — no extension check: [4](#0-3) 

- `contextual_check` runs `HeaderVerifier` against the untampered header (passes, since PoW is valid) — no extension check: [5](#0-4) 

**Failure path permanently poisons the block hash:**

`BlockExtensionVerifier::verify` computes `actual_extra_hash` from the tampered extension and compares it to the value committed in the original header — this fails: [6](#0-5) 

On this error, `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` permanently writes `BLOCK_INVALID` for the block hash: [7](#0-6) 

`insert_block_status` writes to an in-memory `DashMap` only — no persistence, but no expiry either: [8](#0-7) 

**Legitimate block is permanently suppressed:**

When the legitimate block arrives, `contextual_check` immediately returns `BlockIsInvalid`: [9](#0-8) 

The block hash is derived from the header only, not the extension, so the tampered compact block and the legitimate block share the same hash. The node will never accept the legitimate block without a process restart.

**Peer punishment does not undo the damage:** The `verify_callback` in `accept_block` calls `post_sync_process` with `StatusCode::BlockIsInvalid` when verification fails, which may ban the attacker peer. However, the `BLOCK_INVALID` entry is already written before the callback fires, and banning the peer does not clear it. [10](#0-9) 

## Impact Explanation

This is a **Critical** vulnerability matching **"Vulnerabilities which could easily cause consensus deviation."** An attacker who connects to a significant fraction of the network as a relay peer can suppress a specific miner's block from all those nodes simultaneously. Nodes that received the tampered compact block will permanently reject the legitimate block, while nodes that received the legitimate block first will accept it. This creates a persistent chain split between the two populations of nodes — a direct consensus deviation. The attack also matches **"Vulnerabilities which could easily damage CKB economy"** since targeted miners lose block rewards on all poisoned nodes.

## Likelihood Explanation

- Requires only a standard P2P relay connection — no keys, no hashpower, no Sybil attack.
- The attacker must send the tampered compact block before the legitimate one arrives. As a relay peer, the attacker is in the natural relay path and can forward the tampered version immediately upon observing a new block announcement.
- The mutation is a single-bit flip in the extension bytes — trivial to implement.
- The attack is repeatable: the attacker can target every new block from a specific miner.
- The attack is silent from the victim's perspective: the node logs `InvalidExtraHash` but the attacker peer is only punished after the irreversible `BLOCK_INVALID` write has already occurred.

## Recommendation

Before calling `accept_block` with the reconstructed block, verify that the extension bytes in the compact block are consistent with the `extra_hash` committed in the header. This check must be placed after `reconstruct_block` returns `ReconstructionResult::Block`, and must **not** set `BLOCK_INVALID` — only the compact block message is malformed, not the block itself:

```rust
// In compact_block_process.rs, after ReconstructionResult::Block(block):
if let Some(extension) = compact_block.extension() {
    let actual_extra_hash = block.calc_extra_hash().extra_hash();
    if actual_extra_hash != block.extra_hash() {
        // Penalize/ban the peer; do NOT mark BLOCK_INVALID
        return StatusCode::ProtocolMessageIsMalformed
            .with_context("compact block extension does not match header extra_hash");
    }
}
```

The returned `StatusCode::ProtocolMessageIsMalformed` should carry a ban time so the offending peer is punished. The block hash must never be marked `BLOCK_INVALID` in this path because the header (and thus the block hash) is valid — only the compact block message is malformed.

## Proof of Concept

1. Connect to a victim node as a relay peer.
2. Observe a valid mined `BlockV1` with hash `H` being relayed as a `CompactBlockV1`.
3. Intercept or race the compact block message before it reaches the victim.
4. Flip one bit in the `extension` field of the `CompactBlockV1` (all other fields, including the header, remain untouched).
5. Forward the mutated compact block to the victim node.
6. Observe that the victim node logs `InvalidExtraHash` and `ConsumeUnverifiedBlockProcessor` sets `BLOCK_INVALID` for hash `H` in `block_status_map`.
7. Send the legitimate `BlockV1` (or the original compact block) to the victim node via `SendBlock`.
8. Observe that `contextual_check` immediately returns `StatusCode::BlockIsInvalid` for hash `H` and the node never adds the legitimate block to its chain.
9. Confirm that `block_status_map` retains the `BLOCK_INVALID` entry for the lifetime of the process (no restart = no recovery).

### Citations

**File:** sync/src/relayer/mod.rs (L309-332)
```rust
                Err(err) => {
                    error!(
                        "verify block {}-{} failed: {:?}, won't build compact block and broadcast it",
                        block.number(),
                        block.hash(),
                        err
                    );

                    let is_internal_db_error = is_internal_db_error(&err);
                    if is_internal_db_error {
                        return;
                    }

                    // punish the malicious peer
                    post_sync_process(
                        nc.as_ref(),
                        peer_id,
                        &msg_name,
                        StatusCode::BlockIsInvalid.with_context(format!(
                            "block {} is invalid, reason: {}",
                            block.hash(),
                            err
                        )),
                    );
```

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

**File:** sync/src/relayer/compact_block_process.rs (L324-339)
```rust
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
```

**File:** sync/src/relayer/compact_block_verifier.rs (L11-15)
```rust
    pub(crate) fn verify(block: &packed::CompactBlock) -> Status {
        attempt!(PrefilledVerifier::verify(block));
        attempt!(ShortIdsVerifier::verify(block));
        Status::ok()
    }
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

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
    }
```
