### Title
Unchecked Return Value of `GetBlockTransactions` Network Send Silently Stalls Compact Block Reconstruction — (File: `sync/src/relayer/block_transactions_process.rs`)

---

### Summary

In `BlockTransactionsProcess::execute`, when compact block reconstruction is still missing transactions after receiving a `BlockTransactions` message, the node constructs a follow-up `GetBlockTransactions` request and sends it to the peer. The `Status` return value of `async_send_message_to` is explicitly discarded with `let _ignore =`. Regardless of whether the send succeeded, the node immediately overwrites its internal tracking state (`expected_transaction_indexes`, `expected_uncle_indexes`) via `mem::replace`. If the send silently fails, the node holds stale state expecting a `BlockTransactions` reply that will never arrive, stalling compact block reconstruction for that block hash from that peer. Other relay functions in the same codebase consistently check this return value.

---

### Finding Description

In `sync/src/relayer/block_transactions_process.rs`, the `execute` function handles the `BlockTransactions` relay message. When block reconstruction still requires more data (the `ReconstructionResult::Missing` or `ReconstructionResult::Collided` arms), the code builds a `GetBlockTransactions` message and sends it:

```rust
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

let _ignore_prev_value =
    mem::replace(expected_transaction_indexes, missing_transactions);
let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

The `Status` returned by `async_send_message_to` (which wraps `nc.async_send_message_to` and returns `StatusCode::Network` on failure) is bound to `_ignore` and immediately dropped. The state update via `mem::replace` then unconditionally overwrites the pending compact block's expected index lists, recording that a request for those specific indexes has been issued. If the underlying network send failed, no request was actually delivered to the peer, but the node's state reflects that one was.

By contrast, the same relay module's `ask_for_txs` function and the `send_block_proposals` utility both check the returned `Status` and log errors on failure:

```rust
// sync/src/relayer/mod.rs – ask_for_txs
let status = async_send_message_to(nc, peer, &message).await;
if !status.is_ok() {
    ckb_logger::error!("interrupted request for transactions, status: {:?}", status);
}
```

```rust
// sync/src/utils.rs – send_block_proposals
let status = async_quick_send_message_to(nc, peer_index, &message).await;
if !status.is_ok() {
    ckb_logger::error!("send RelayBlockProposal to {}, status: {:?}", peer_index, status);
}
```

The inconsistency is a direct structural analog to the original report: `safeTransfer` is used in some places while a bare unchecked `transfer` is used in another.

---

### Impact Explanation

When `async_send_message_to` fails (e.g., the peer's send buffer is full, the session is being torn down, or the underlying p2p control channel returns an error), the `GetBlockTransactions` message is never delivered. The node has already replaced `expected_transaction_indexes` and `expected_uncle_indexes` with the new missing-index lists, so it will wait indefinitely for a `BlockTransactions` reply from that peer that will never arrive. Compact block reconstruction for that block hash is stalled from that peer's perspective. The block cannot be accepted until either a timeout expires and the node re-requests from another peer, or the block arrives via a full `SendBlock` message. This delays block propagation and can affect mining revenue and transaction confirmation latency across the network.

---

### Likelihood Explanation

Any unprivileged relay peer can trigger this condition by sending a compact block whose short IDs do not fully resolve from the local tx-pool, causing the node to enter the `Missing` or `Collided` reconstruction path. The subsequent `GetBlockTransactions` send can fail under normal network conditions (peer disconnects, p2p send queue full, session closed mid-flight). No special privileges or cryptographic material are required. The attacker-controlled entry path is: connect as a relay peer → send a crafted or legitimate compact block with unresolvable short IDs → disconnect or saturate the send queue at the moment the node attempts to send `GetBlockTransactions`.

---

### Recommendation

Replace the discarded result with an explicit status check, consistent with `ask_for_txs` and `send_block_proposals`. If the send fails, the node should either skip the `mem::replace` state update (so the old expected indexes remain valid for a retry) or log the failure and allow the pending compact block entry to be cleaned up by the existing timeout mechanism:

```rust
// Instead of:
let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

// Use:
let status = async_send_message_to(&self.nc, self.peer, &message).await;
if !status.is_ok() {
    ckb_logger::error!(
        "GetBlockTransactions send failed for {}, peer {}: {:?}",
        block_hash, self.peer, status
    );
    // Do not update expected_transaction_indexes / expected_uncle_indexes
    // so the existing state remains valid for a future retry or timeout.
    return StatusCode::Network.with_context("GetBlockTransactions send failed");
}
```

---

### Proof of Concept

**Root cause location:** [1](#0-0) 

**Inconsistency — `ask_for_txs` checks the result:** [2](#0-1) 

**Inconsistency — `send_block_proposals` checks the result:** [3](#0-2) 

**`async_send_message_to` returns a `Status` that encodes network failure:** [4](#0-3) 

**Attack entry path — relay peer sends `BlockTransactions` message, triggering `execute`:** [5](#0-4)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L45-63)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
        let active_chain = shared.active_chain();
        let block_transactions = self.message.to_entity();
        let block_hash = block_transactions.block_hash();
        let received_transactions: Vec<core::TransactionView> = block_transactions
            .transactions()
            .into_iter()
            .map(|tx| tx.into_view())
            .collect();
        let received_uncles: Vec<core::UncleBlockView> = block_transactions
            .uncles()
            .into_iter()
            .map(|uncle| uncle.into_view())
            .collect();

        let mut missing_transactions: Vec<u32>;
        let mut missing_uncles: Vec<u32>;
        let mut collision = false;
```

**File:** sync/src/relayer/block_transactions_process.rs (L167-178)
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
```

**File:** sync/src/relayer/mod.rs (L619-625)
```rust
                let status = async_send_message_to(nc, peer, &message).await;
                if !status.is_ok() {
                    ckb_logger::error!(
                        "interrupted request for transactions, status: {:?}",
                        status
                    );
                }
```

**File:** sync/src/utils.rs (L150-157)
```rust
pub(crate) async fn async_send_message_to<Message: Entity>(
    nc: &Arc<dyn CKBProtocolContext + Sync>,
    peer_index: PeerIndex,
    message: &Message,
) -> Status {
    let protocol_id = nc.protocol_id();
    async_send_message(protocol_id, nc, peer_index, message).await
}
```

**File:** sync/src/utils.rs (L247-254)
```rust
    let status = async_quick_send_message_to(nc, peer_index, &message).await;
    if !status.is_ok() {
        ckb_logger::error!(
            "send RelayBlockProposal to {}, status: {:?}",
            peer_index,
            status
        );
    }
```
