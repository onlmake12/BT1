### Title
Compact Block Reconstruction Permanently Stuck on Single Unresponsive Peer with No Timeout — (`sync/src/relayer/compact_block_process.rs`)

---

### Summary

When a compact block arrives with missing transactions, `GetBlockTransactions` is sent exclusively to the **single peer** that sent the compact block. There is no timeout, no retry to alternative peers, and no time-based expiry for `pending_compact_blocks` entries. A malicious relay peer can exploit this to permanently stall block reconstruction for the duration of an epoch (~4 hours), preventing block propagation and causing miners' blocks to be orphaned.

---

### Finding Description

**Step 1 — Missing transactions trigger a single-peer request.**

When `CompactBlockProcess::execute` cannot reconstruct a block, it calls `missing_or_collided_post_process`: [1](#0-0) 

Inside that function, `GetBlockTransactions` is sent **only to `peer`** — the single peer that sent the compact block: [2](#0-1) 

No other connected peer is ever asked for the missing transactions.

**Step 2 — The pending entry has no time-based expiry.**

The entry is inserted into `pending_compact_blocks` with a timestamp (`unix_time_as_millis()`), but that timestamp is **never read back** for timeout enforcement: [3](#0-2) 

The only cleanup that occurs is epoch-based — entries are pruned when a **successfully reconstructed block** from a newer epoch is accepted: [4](#0-3) 

Within the same epoch (up to ~4 hours), a pending entry that never receives a `BlockTransactions` response is **never evicted**.

**Step 3 — `BlockTransactionsProcess` also only retries the same peer.**

When a partial `BlockTransactions` response arrives and reconstruction still fails, the follow-up `GetBlockTransactions` is again sent only to `self.peer`: [5](#0-4) 

---

### Impact Explanation

A malicious relay peer that:
1. Receives a compact block from a miner,
2. Forwards it (without the missing transactions) to victim nodes, and
3. Silently drops all `GetBlockTransactions` requests,

causes those victim nodes to hold the compact block in `pending_compact_blocks` for the remainder of the epoch with no resolution. The block is never reconstructed, never forwarded to downstream peers, and the miner's block is effectively orphaned from the perspective of those nodes. If the malicious peer is positioned between the miner and a significant portion of the network, this constitutes a targeted block-withholding / orphaning attack requiring no PoW and no privileged access.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged P2P peer can relay a compact block and then ignore `GetBlockTransactions`. No key, no PoW, no operator role required.
- **Barrier**: The attacker must be in the relay path between a miner and victim nodes — achievable by any well-connected peer.
- **Duration**: The stuck state persists for up to one full epoch (~4 hours) with no automatic recovery.
- **No fallback within the relay protocol**: The sync protocol (`GetBlocks`/`SendBlock`) can eventually deliver the block, but only if another peer independently has the full block and proactively sends it — there is no timer that triggers this fallback.

---

### Recommendation

1. **Add a time-based expiry** to `pending_compact_blocks` entries. The stored `unix_time_as_millis()` timestamp should be checked periodically (e.g., in the relay `notify` loop) and stale entries evicted after a configurable deadline (e.g., 30–60 seconds).
2. **On expiry, broadcast `GetBlockTransactions` to all peers** that have announced the same block hash, not just the original sender.
3. **Alternatively**, on expiry, issue a `GetBlocks` request to other peers as a fallback to fetch the full block via the sync protocol.

---

### Proof of Concept

1. Connect to a CKB node as a peer supporting `RelayV3`.
2. Obtain a valid compact block (e.g., by observing one from a miner on the network).
3. Relay the compact block to the victim node, omitting the transactions that are not in its tx-pool (i.e., send a compact block whose `short_ids` include transactions the victim does not have).
4. The victim node sends `GetBlockTransactions` back to your peer.
5. Drop the `GetBlockTransactions` message — send no `BlockTransactions` response.
6. Observe that the victim node's `pending_compact_blocks` retains the entry for the full epoch duration with no retry to any other peer.
7. The block is never reconstructed or forwarded; the miner's block is orphaned from the victim's perspective.

The root cause is in `missing_or_collided_post_process` at: [6](#0-5) 

and the absence of any timeout-driven eviction or peer-rotation logic for entries in `pending_compact_blocks`.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L106-117)
```rust
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
```

**File:** sync/src/relayer/compact_block_process.rs (L344-379)
```rust
/// request missing txs and uncles from peer
async fn missing_or_collided_post_process(
    compact_block: CompactBlock,
    block_hash: Byte32,
    shared: &SyncShared,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    missing_transactions: Vec<u32>,
    missing_uncles: Vec<u32>,
    peer: PeerIndex,
) {
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));

    let content = packed::GetBlockTransactions::new_builder()
        .block_hash(block_hash)
        .indexes(missing_transactions.as_slice())
        .uncle_indexes(missing_uncles.as_slice())
        .build();
    let message = packed::RelayMessage::new_builder().set(content).build();
    shared.shared().async_handle().spawn(async move {
        let sending = async_send_message_to(&nc, peer, &message).await;
        if !sending.is_ok() {
            ckb_logger::warn_target!(
                crate::LOG_TARGET_RELAY,
                "ignore the sending message error, error: {}",
                sending
            );
        }
    });
}
```

**File:** sync/src/relayer/block_transactions_process.rs (L165-185)
```rust
                assert!(!missing_transactions.is_empty() || !missing_uncles.is_empty());

                let content = packed::GetBlockTransactions::new_builder()
                    .block_hash(block_hash.clone())
                    .indexes(missing_transactions.as_slice())
                    .uncle_indexes(missing_uncles.as_slice())
                    .build();
                let message = packed::RelayMessage::new_builder().set(content).build();

                let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

                let _ignore_prev_value =
                    mem::replace(expected_transaction_indexes, missing_transactions);
                let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);

                if collision {
                    return StatusCode::CompactBlockMeetsShortIdsCollision.with_context(block_hash);
                } else {
                    return StatusCode::CompactBlockRequiresFreshTransactions
                        .with_context(block_hash);
                }
```
