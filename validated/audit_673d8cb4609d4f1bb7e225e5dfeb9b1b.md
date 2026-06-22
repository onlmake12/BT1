### Title
Silently Ignored `async_send_message_to` Return Value Causes Inconsistent Compact-Block Reconstruction State — (File: `sync/src/relayer/block_transactions_process.rs`)

### Summary
In `block_transactions_process.rs`, after computing missing transactions needed to reconstruct a compact block, the node sends a `GetBlockTransactions` request to the relaying peer. The `Result` returned by `async_send_message_to` is discarded with `let _ignore = ...`. Immediately after, the node unconditionally overwrites its internal `expected_transaction_indexes` and `expected_uncle_indexes` state to reflect the new missing set. If the send silently fails, the node's in-memory state records that it is waiting for transactions it never actually requested, permanently stalling compact-block reconstruction for that block from that peer.

### Finding Description
In `sync/src/relayer/block_transactions_process.rs`, after determining which transactions are missing from a compact block, the code builds a `GetBlockTransactions` message and sends it:

```rust
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

let _ignore_prev_value =
    mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

The `async_send_message_to` helper (defined in `sync/src/utils.rs`) returns a `Status` value that encodes success or a `StatusCode::Network` error. By binding it to `_ignore`, the caller never inspects whether the network send succeeded. The two `mem::replace` calls that follow execute unconditionally, updating the node's tracked "expected" indexes regardless of whether the request was actually delivered.

The `async_send_message_to` function itself does log an error on failure, but it returns that error as a `Status` value that is never propagated or acted upon here. [1](#0-0) [2](#0-1) 

### Impact Explanation
When the send fails silently:

1. `expected_transaction_indexes` is overwritten with the new missing set.
2. The node believes it has an outstanding `GetBlockTransactions` request in flight.
3. No retry or timeout mechanism re-issues the request because the state already reflects the "sent" condition.
4. The compact block for that hash can never be reconstructed from this peer; the block is effectively dropped from this relay path.

A peer that triggers repeated compact-block announcements with crafted short-ID collisions (the `ReconstructionResult::Collided` branch at line 151) combined with transient network-layer send failures can force a target node to permanently stall reconstruction of specific blocks, degrading block propagation and potentially delaying the node's view of the canonical tip. [3](#0-2) 

### Likelihood Explanation
The send path goes through `nc.async_send_message`, which can return an error whenever the underlying p2p session is closing or the send buffer is full. Any unprivileged peer that is already connected and relaying compact blocks can trigger this condition by closing the session immediately after sending the compact block, causing the subsequent `GetBlockTransactions` send to fail. No special privileges, keys, or majority hash power are required.

### Recommendation
Check the return value of `async_send_message_to` before updating `expected_transaction_indexes` and `expected_uncle_indexes`. If the send fails, either return an error status (causing the caller to ban or disconnect the peer) or leave the expected indexes unchanged so a subsequent retry can re-issue the request:

```rust
let status = async_send_message_to(&self.nc, self.peer, &message).await;
if status.is_ok() {
    *expected_transaction_indexes = missing_transactions;
    *expected_uncle_indexes = missing_uncles;
} else {
    return status;
}
```

The same pattern should be applied to the analogous `let _ignore` in `sync/src/synchronizer/get_headers_process.rs` (IBD `send_in_ibd` path) and `sync/src/synchronizer/mod.rs` (`find_blocks_to_fetch` fetch-channel send). [4](#0-3) [5](#0-4) 

### Proof of Concept

1. Attacker connects to a CKB node as a relay peer.
2. Attacker sends a `CompactBlock` message whose short IDs do not match any transactions in the node's pool, triggering `ReconstructionResult::Missing` or `ReconstructionResult::Collided`.
3. The node enters the branch at line 167 and constructs a `GetBlockTransactions` message.
4. Attacker immediately closes the p2p session (or fills the send buffer) so that `async_send_message_to` returns `StatusCode::Network`.
5. Because the return value is discarded, `expected_transaction_indexes` is still overwritten at line 176-177.
6. The node now holds stale "expected" state for that block hash with no outstanding request. The compact block is permanently unresolvable from this peer without an external trigger (e.g., a full `SendBlock` from a different peer).
7. Repeating across multiple peers and block hashes degrades the node's ability to stay at the chain tip. [6](#0-5)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L151-185)
```rust
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

**File:** sync/src/utils.rs (L72-87)
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
```

**File:** sync/src/synchronizer/get_headers_process.rs (L101-115)
```rust
    fn send_in_ibd(&self) {
        let content = packed::InIBD::new_builder().build();
        let message = packed::SyncMessage::new_builder().set(content).build();
        let nc = Arc::clone(self.nc);
        let peer = self.peer;
        self.synchronizer
            .shared()
            .shared()
            .async_handle()
            .spawn(async move {
                let _ignore =
                    async_send_message(SupportProtocols::Sync.protocol_id(), &nc, peer, &message)
                        .await;
            });
    }
```

**File:** sync/src/synchronizer/mod.rs (L795-801)
```rust
                    if !sender.is_full() {
                        let peers = self.get_peers_to_fetch(ibd, &disconnect_list);
                        let _ignore = sender.try_send(FetchCMD {
                            peers,
                            ibd_state: ibd,
                        });
                    }
```
