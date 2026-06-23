### Title
Unchecked Return Value of `async_send_message_to` Causes Stale Compact-Block Sync State — (`sync/src/relayer/block_transactions_process.rs`)

---

### Summary

In `BlockTransactionsProcess::execute()`, when compact-block reconstruction fails due to missing transactions, the node sends a `GetBlockTransactions` request to the peer and then **unconditionally** overwrites its internal expected-transaction-index state — regardless of whether the network send succeeded. If the send silently fails, the node's sync state diverges from reality: it believes it has requested specific transactions, but the peer never received the request. The node then waits indefinitely for a `BlockTransactions` response that will never arrive, stalling compact-block reconstruction for that block from that peer.

---

### Finding Description

In `BlockTransactionsProcess::execute()`, after a compact block cannot be fully reconstructed, the code sends a follow-up `GetBlockTransactions` message and then replaces the tracked expected indexes:

```rust
// sync/src/relayer/block_transactions_process.rs, lines 174–178
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

let _ignore_prev_value =
    mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

The return value of `async_send_message_to` is explicitly discarded with `let _ignore`. The state mutation on lines 176–178 then executes unconditionally. If the send returns an error (peer disconnected mid-flight, send buffer full, transport error), the node has:

1. Updated `expected_transaction_indexes` and `expected_uncle_indexes` inside the `pending_compact_blocks` map to reflect the new request.
2. Never actually delivered the `GetBlockTransactions` message to the peer.

The peer will not send a `BlockTransactions` reply. The node's entry in `pending_compact_blocks` for this block hash now holds stale expected indexes that no peer will ever satisfy from this peer's side. [1](#0-0) 

The correct pattern used elsewhere in the codebase (e.g., `send_getblocks`) is to log or handle the error rather than silently discard it:

```rust
// sync/src/synchronizer/mod.rs, lines 343–352
if let Err(err) = Into::<ServiceAsyncControl>::into(nc)
    .send_message_to(...)
    .await
{
    debug!("synchronizer sending GetBlocks error: {:?}", err);
}
``` [2](#0-1) 

---

### Impact Explanation

The node's internal compact-block sync state (`pending_compact_blocks` → `peers_map` → `(expected_transaction_indexes, expected_uncle_indexes)`) becomes permanently inconsistent for the affected `(block_hash, peer)` pair. The node will not re-issue the `GetBlockTransactions` request to that peer because it believes the request was already sent and is awaiting a reply. Block reconstruction from that peer is stalled for the lifetime of the pending entry. If multiple peers trigger this condition simultaneously (e.g., during a burst of compact-block relays under network stress), block propagation latency increases across the board. While the node can still receive the block from other peers, the degraded relay path reduces the efficiency of the compact-block relay protocol and can delay tip advancement. [3](#0-2) 

---

### Likelihood Explanation

Network send failures are a normal occurrence in any P2P network: peers disconnect, send buffers fill, or transient transport errors occur. An unprivileged remote peer can trigger this condition by:

1. Sending a valid compact block that references transactions not in the victim's mempool.
2. Disconnecting (or allowing the connection to drop) at the moment the victim attempts to send `GetBlockTransactions`.

The victim's send fails, the state is corrupted, and the peer is gone — so no recovery is possible from that peer. This is reachable by any peer that can relay compact blocks, which is every connected sync peer.

---

### Recommendation

Check the return value of `async_send_message_to` before mutating state. Only update `expected_transaction_indexes` and `expected_uncle_indexes` if the send succeeded. If the send fails, either leave the state unchanged (so a future retry can re-derive the missing set) or remove the peer's entry from `peers_map` entirely and log the error:

```rust
if let Err(e) = async_send_message_to(&self.nc, self.peer, &message).await {
    warn!("Failed to send GetBlockTransactions to peer {}: {:?}", self.peer, e);
    // Do NOT update expected indexes; leave state for retry or cleanup
    return StatusCode::Network.with_context(format!("send failed: {e}"));
}
// Only update state after confirmed send
let _ignore_prev_value = mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

---

### Proof of Concept

1. Attacker peer connects to the victim CKB node via the relay protocol.
2. Attacker sends a `CompactBlock` message referencing transactions not present in the victim's mempool.
3. Victim enters `BlockTransactionsProcess::execute()`, determines transactions are missing, builds a `GetBlockTransactions` message.
4. Attacker closes the TCP connection at the moment the victim calls `async_send_message_to` — the send returns an error.
5. Despite the error, lines 176–178 execute: `expected_transaction_indexes` is replaced with `missing_transactions` and `expected_uncle_indexes` is replaced with `missing_uncles`.
6. The victim's `pending_compact_blocks` entry for this block hash now records that it is waiting for specific transactions from this peer — but the peer is gone and never received the request.
7. No further `BlockTransactions` message will arrive from this peer. The compact-block reconstruction for this block from this peer is permanently stalled until the pending entry is garbage-collected by an unrelated timeout or cleanup path. [4](#0-3)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L65-186)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
            let (compact_block, peers_map, _) = pending.get_mut();
            if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
                let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
                ckb_logger::info!(
                    "relayer receive BLOCKTXN of {}, peer: {}",
                    block_hash,
                    self.peer
                );

                attempt!(BlockTransactionsVerifier::verify(
                    compact_block,
                    expected_transaction_indexes,
                    &received_transactions,
                ));
                attempt!(BlockUnclesVerifier::verify(
                    compact_block,
                    expected_uncle_indexes,
                    &received_uncles,
                ));

                let ret = self
                    .relayer
                    .reconstruct_block(
                        &active_chain,
                        compact_block,
                        received_transactions,
                        expected_uncle_indexes,
                        &received_uncles,
                    )
                    .await;

                // Request proposal
                {
                    let proposals: Vec<_> = received_uncles
                        .into_iter()
                        .flat_map(|u| u.data().proposals().into_iter())
                        .collect();
                    self.relayer.request_proposal_txs(
                        &self.nc,
                        self.peer,
                        (
                            compact_block.header().into_view().number(),
                            block_hash.clone(),
                        )
                            .into(),
                        proposals,
                    );
                }

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
            }
```

**File:** sync/src/synchronizer/mod.rs (L343-352)
```rust
        if let Err(err) = Into::<ServiceAsyncControl>::into(nc)
            .send_message_to(
                peer,
                SupportProtocols::Sync.protocol_id(),
                message.as_bytes(),
            )
            .await
        {
            debug!("synchronizer sending GetBlocks error: {:?}", err);
        }
```
