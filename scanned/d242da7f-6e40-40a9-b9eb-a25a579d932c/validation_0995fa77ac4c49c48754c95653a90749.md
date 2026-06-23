### Title
Unchecked Return Value of `async_send_message_to` Silently Drops Missing-Transaction Requests During Compact Block Reconstruction — (`sync/src/relayer/block_transactions_process.rs`)

---

### Summary

In `block_transactions_process.rs`, when a compact block cannot be reconstructed due to missing transactions or a short-ID collision, the node sends a `GetBlockTransactions` request back to the peer. The return value of `async_send_message_to` is explicitly discarded with `let _ignore = ...`. If the network send fails, the node silently stores the pending compact block state but never actually dispatches the follow-up request, causing the compact block reconstruction to stall with no error surfaced to the caller.

---

### Finding Description

In `BlockTransactionsProcess::execute()`, after determining that transactions are missing or short IDs collided, the code calls `missing_or_collided_post_process`, which stores the pending compact block state and then attempts to send a `GetBlockTransactions` message to the peer: [1](#0-0) 

The spawned async task calls `async_send_message_to` and assigns the result to `_ignore`, explicitly discarding the `Status` return value:

```rust
let sending = async_send_message_to(&nc, peer, &message).await;
if !sending.is_ok() {
    ckb_logger::warn_target!(
        crate::LOG_TARGET_RELAY,
        "ignore the sending message error, error: {}",
        sending
    );
}
```

Wait — looking at `compact_block_process.rs` lines 369–378, the error *is* logged with a warning. However, in `block_transactions_process.rs` at line 174, the result is discarded with no logging at all: [2](#0-1) 

```rust
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;
```

The `async_send_message_to` utility itself logs errors internally: [3](#0-2) 

However, the `Status` error is still discarded at the call site. The pending compact block entry has already been inserted into `pending_compact_blocks` at this point: [4](#0-3) 

If the send fails, the pending state is stored but no request is dispatched. The node waits for a `BlockTransactions` response that will never arrive from this peer.

---

### Impact Explanation

When a compact block arrives with missing transactions, the node:
1. Stores the pending compact block state (with the list of missing transaction indexes).
2. Sends `GetBlockTransactions` to the peer.

If step 2 silently fails, the pending state is orphaned. The block cannot be accepted until either:
- Another peer sends the same compact block (triggering a new request), or
- The pending compact block is cleaned up by the expiry timer.

This causes a **liveness/availability impact**: block propagation stalls for the affected compact block from the affected peer. In a scenario where a single peer is the sole provider of a new block (e.g., the miner's direct relay peer), a transient network failure during the `GetBlockTransactions` send causes the block to be delayed until the pending entry expires or another peer relays it.

---

### Likelihood Explanation

The failure path is triggered when `nc.async_send_message` returns an error, which can happen during transient network disruptions, peer disconnection races, or when the network layer's send buffer is full. This is an externally reachable path: any block relayer peer that sends a compact block with short IDs not in the local mempool triggers this code. The likelihood is low-to-medium — network errors are uncommon but realistic under load or adversarial conditions.

---

### Recommendation

In `block_transactions_process.rs`, propagate the send failure status back to the caller instead of discarding it, consistent with how `compact_block_process.rs` handles the same scenario:

```diff
- let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;
+ let status = async_send_message_to(&self.nc, self.peer, &message).await;
+ if !status.is_ok() {
+     return status;
+ }
``` [5](#0-4) 

---

### Proof of Concept

1. Peer A sends a `CompactBlock` to the local node. The compact block contains short IDs that are not in the local mempool.
2. The node enters `ReconstructionResult::Missing`, calls `missing_or_collided_post_process`, and spawns an async task to send `GetBlockTransactions` back to Peer A.
3. At the moment the async task executes, Peer A has disconnected (or the send buffer is full). `async_send_message_to` returns a non-OK `Status`.
4. The `_ignore` binding discards this status. The pending compact block entry remains in `pending_compact_blocks` with the missing transaction indexes recorded.
5. No `GetBlockTransactions` message is ever sent. The node waits indefinitely for a `BlockTransactions` response that never arrives, stalling compact block reconstruction for this block until the pending entry expires or another peer relays the block. [6](#0-5) [7](#0-6)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L128-185)
```rust
                        // We need to get all transactions and uncles that do not exist locally
                        // at once to restore a block
                        //
                        // Under normal circumstances, one request is enough, when the chain occurs fork,
                        // the transaction pool may drop some transactions due to double spend check, at
                        // this time, the previously issued request to obtain transactions may not meet
                        // the needs of a one-time construction, we need to send another complete request
                        // to do so. That is, the current miss + the miss of the previous request are
                        // combined and requested once. This is a small probability event
                        missing_transactions = transactions
                            .into_iter()
                            .map(|i| i as u32)
                            .chain(expected_transaction_indexes.iter().copied())
                            .collect();
                        missing_uncles = uncles
                            .into_iter()
                            .map(|i| i as u32)
                            .chain(expected_uncle_indexes.iter().copied())
                            .collect();

                        missing_transactions.sort_unstable();
                        missing_uncles.sort_unstable();
                    }
                    ReconstructionResult::Collided => {
                        missing_transactions = compact_block
                            .short_id_indexes()
                            .into_iter()
                            .map(|i| i as u32)
                            .collect();
                        collision = true;
                        missing_uncles = vec![];
                    }
                    ReconstructionResult::Error(status) => {
                        return status;
                    }
                }

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

**File:** sync/src/relayer/block_transactions_process.rs (L354-361)
```rust

```

**File:** sync/src/relayer/block_transactions_process.rs (L363-378)
```rust

```

**File:** sync/src/utils.rs (L72-101)
```rust
pub(crate) async fn async_send_message<Message: Entity>(
    protocol_id: ProtocolId,
    nc: &Arc<dyn CKBProtocolContext + Sync>,
    peer_index: PeerIndex,
    message: &Message,
) -> Status {
    // ignore Error return, only happens on shutdown case
    if let Err(err) = nc
        .async_send_message(protocol_id, peer_index, message.as_bytes())
        .await
    {
        let name = message_name(protocol_id, message);
        let error_message = format!("nc.send_message {name}, error: {err:?}");
        ckb_logger::error!("{}", error_message);
        return StatusCode::Network.with_context(error_message);
    }

    let bytes = message.as_bytes().len() as u64;
    let item_name = item_name(protocol_id, message);
    let protocol_name = protocol_name(protocol_id);
    metric_ckb_message_bytes(
        MetricDirection::Out,
        &protocol_name,
        &item_name,
        None,
        bytes,
    );

    Status::ok()
}
```
