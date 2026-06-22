### Title
Silently Ignored Return Value of `async_send_message_to` Causes Compact Block Reconstruction to Stall — (`sync/src/relayer/block_transactions_process.rs`)

---

### Summary

In `sync/src/relayer/block_transactions_process.rs`, the return value of `async_send_message_to` is explicitly discarded with `let _ignore = ...` when requesting missing transactions needed to reconstruct a compact block. If the send fails, the node has already mutated its pending compact block state (recording which transactions are missing and from which peer), but the `GetBlockTransactions` network message is never actually delivered. The peer never receives the request, so the missing transactions are never sent back, and compact block reconstruction stalls until the pending entry times out.

---

### Finding Description

When a `BlockTransactions` message arrives and the compact block still cannot be reconstructed (either due to missing transactions or a short-ID collision), the node:

1. Updates `pending_compact_blocks` with the new set of missing transaction indexes.
2. Builds a `GetBlockTransactions` message.
3. Calls `async_send_message_to` to send it to the peer — **and discards the result**. [1](#0-0) 

```rust
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

let _ignore_prev_value =
    mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

The state mutation (updating `expected_transaction_indexes` and `expected_uncle_indexes`) happens unconditionally after the send, regardless of whether the send succeeded. If `async_send_message_to` returns an error (e.g., the peer's send buffer is full, the connection was dropped, or the network layer is under pressure), the node silently proceeds: the pending compact block entry now records the updated missing-transaction set, but the peer was never asked to supply those transactions.

The `async_send_message_to` helper itself does propagate errors as a `Status` return value: [2](#0-1) 

…but the call site in `block_transactions_process.rs` throws that `Status` away entirely.

Compare this with the analogous path in `compact_block_process.rs`, where the same `async_send_message_to` result is at least inspected and a warning is emitted: [3](#0-2) 

```rust
let sending = async_send_message_to(&nc, peer, &message).await;
if !sending.is_ok() {
    ckb_logger::warn_target!(...);
}
```

The `block_transactions_process.rs` path has no such check.

---

### Impact Explanation

When the send silently fails:

- The node's `pending_compact_blocks` map holds a stale entry for the block hash, recording that it is waiting for specific transactions from a specific peer.
- The peer never receives the `GetBlockTransactions` request and therefore never sends the missing transactions.
- Compact block reconstruction for that block hash is stuck until the pending entry ages out (the entry stores a `unix_time_as_millis()` timestamp, implying a timeout sweep exists, but the block is not reconstructed in the interim).
- During the stall window, the node cannot accept the block from that peer, delaying tip advancement and block propagation.

This is a **liveness / block-propagation** impact: the node fails to make progress on block reconstruction for a period proportional to the pending-entry timeout, without any log warning or error to indicate why.

---

### Likelihood Explanation

The entry path is fully attacker-reachable:

1. An unprivileged peer sends a `CompactBlock` message that references transactions not in the local tx-pool (triggering the `Missing` branch).
2. The node sends `GetBlockTransactions` to the peer (first request — this one is sent correctly from `compact_block_process.rs`).
3. The peer responds with a `BlockTransactions` message that is still incomplete (e.g., it omits some transactions, or a short-ID collision is declared).
4. `BlockTransactionsProcess::execute` runs, hits the retry path at line 174, and the `_ignore`d send fails (e.g., because the peer's send queue is full or the connection is momentarily disrupted).
5. The node is now stuck waiting for transactions it never requested.

No privileged access, no majority hashpower, and no Sybil attack is required. A single misbehaving or slow peer can trigger this path.

---

### Recommendation

Check the return value of `async_send_message_to` at line 174 and handle failure explicitly. At minimum, log a warning so operators can observe the failure. Ideally, if the send fails, either:

- Do **not** update `expected_transaction_indexes` / `expected_uncle_indexes` (so the old request remains valid), or
- Remove the pending compact block entry so the block can be re-requested cleanly on the next compact block announcement.

```rust
// Recommended: check and handle the error
if let Err(e) = async_send_message_to(&self.nc, self.peer, &message).await {
    warn_target!(LOG_TARGET_RELAY,
        "failed to send GetBlockTransactions for {}: {}", block_hash, e);
    // optionally: remove pending entry or skip state mutation
    return StatusCode::Network.with_context(block_hash);
}
// Only mutate state after confirmed send
let _ignore_prev_value = mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

---

### Proof of Concept

1. Connect a crafted peer to a CKB node.
2. Send a `CompactBlock` with short IDs that are not in the node's tx-pool.
3. The node sends `GetBlockTransactions` (first request, correctly sent).
4. Reply with a `BlockTransactions` message that is still missing some transactions.
5. At the moment the node processes this reply, arrange for the peer's send buffer to be full (e.g., flood the peer's inbound queue with other messages) so that the second `async_send_message_to` at line 174 returns an error.
6. Observe: the node's `pending_compact_blocks` entry is updated with the new missing-transaction set, but no `GetBlockTransactions` message is delivered to the peer. The block reconstruction stalls silently. [4](#0-3)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L167-185)
```rust
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

**File:** sync/src/relayer/compact_block_process.rs (L369-378)
```rust
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
```
