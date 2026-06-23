### Title
Silently Ignored `GetBlockTransactions` Send Result Causes Inconsistent Pending-Block State — (`File: sync/src/relayer/block_transactions_process.rs`)

---

### Summary

In `BlockTransactionsProcess::execute()`, when a compact block cannot be reconstructed due to missing transactions, the node sends a `GetBlockTransactions` request to the peer. The result of that send is explicitly discarded with `let _ignore = ...`, but the node's internal pending state (`expected_transaction_indexes`, `expected_uncle_indexes`) is unconditionally updated immediately after. If the send fails, the node records that it has requested specific missing transactions from the peer — but the peer never received the request — causing a block reconstruction stall until the pending entry times out.

---

### Finding Description

In `sync/src/relayer/block_transactions_process.rs`, the `execute()` function handles the `BlockTransactions` relay message. When block reconstruction is incomplete (missing transactions or uncles), the node builds a `GetBlockTransactions` message and sends it back to the peer:

```rust
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

let _ignore_prev_value =
    mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
``` [1](#0-0) 

The `async_send_message_to` utility function returns a `Status` that encodes success or failure of the network send. [2](#0-1) 

By binding the result to `_ignore`, the caller unconditionally proceeds to overwrite `expected_transaction_indexes` and `expected_uncle_indexes` in the `pending_compact_blocks` map regardless of whether the message was actually delivered. [3](#0-2) 

This is structurally identical to the original vulnerability class: a critical operation's return value is discarded, leaving persistent state inconsistent with the actual outcome of the operation.

Compare with the analogous `missing_or_collided_post_process` path in `compact_block_process.rs`, which at least logs a warning when the send fails — but still does not abort the state update: [4](#0-3) 

The `pending_compact_blocks` map entry stores a timestamp for eventual cleanup, but the stall window between the failed send and the timeout expiry is the attack surface. [5](#0-4) 

---

### Impact Explanation

When the send fails silently:

1. The node's `pending_compact_blocks` entry for the block hash records `expected_transaction_indexes = missing_transactions` — as if the request was successfully dispatched.
2. The peer never receives the `GetBlockTransactions` request and therefore never sends the `BlockTransactions` response.
3. The node waits for a response that will never arrive, stalling compact block reconstruction for that `(block_hash, peer)` pair until the pending entry is evicted by the timeout cleanup sweep.
4. During this stall window, the node cannot accept the block from this peer via the compact block relay path, delaying block propagation and potentially causing the node to fall behind the chain tip.

---

### Likelihood Explanation

The `async_send_message_to` send can fail when the peer's send buffer is full (`SendErrorKind::BrokenPipe` or equivalent). An unprivileged relay peer can induce this condition by:

- Sending a compact block that requires fresh transactions (triggering the `Missing` reconstruction path).
- Simultaneously flooding the node with other relay messages (e.g., `RelayTransactionHashes`, `GetRelayTransactions`) to saturate the outbound send queue for that peer session.

This is reachable by any peer connected to the relay protocol (`SupportProtocols::RelayV3`). No privileged role, key material, or majority hashpower is required. The attacker only needs to be a connected relay peer and control the timing of their compact block submission relative to their flooding traffic.

---

### Recommendation

Check the return value of `async_send_message_to` before updating `expected_transaction_indexes` and `expected_uncle_indexes`. If the send fails, either:

1. **Do not update the pending state** — leave the previous expected indexes intact so a subsequent retry or a different peer can still satisfy the request; or
2. **Remove the pending entry entirely** — forcing the compact block to be re-requested from scratch on the next relay cycle.

```rust
// Instead of:
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;
let _ignore_prev_value = mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);

// Use:
let status = async_send_message_to(&self.nc, self.peer, &message).await;
if status.is_ok() {
    mem::replace(expected_transaction_indexes, missing_transactions);
    mem::replace(expected_uncle_indexes, missing_uncles);
} else {
    // log and return error status without mutating pending state
    return status;
}
```

Apply the same fix to the `missing_or_collided_post_process` helper in `compact_block_process.rs`, which has the same structural issue. [6](#0-5) 

---

### Proof of Concept

1. Attacker connects to a CKB node as a relay peer.
2. Attacker sends a valid compact block whose short IDs do not match any transactions in the node's tx-pool, triggering `ReconstructionResult::Missing`.
3. Simultaneously, attacker floods the node with `RelayTransactionHashes` messages to fill the outbound send buffer for that peer session.
4. The node enters `BlockTransactionsProcess::execute()` → `Missing` branch → calls `async_send_message_to` which fails (buffer full / broken pipe).
5. `let _ignore` discards the error; `mem::replace` updates `expected_transaction_indexes` with the new missing list.
6. The peer never receives `GetBlockTransactions`; the node's pending entry now records the wrong expected state.
7. The node waits for a `BlockTransactions` response that never arrives, stalling reconstruction of that block from this peer for the full pending-compact-block timeout window. [7](#0-6)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L120-185)
```rust
                match ret {
                    ReconstructionResult::Block(block) => {
                        pending.remove();
                        self.relayer
                            .accept_block(self.nc, self.peer, block, "BlockTransactions");
                        return Status::ok();
                    }
                    ReconstructionResult::Missing(transactions, uncles) => {
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

**File:** sync/src/utils.rs (L72-100)
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
