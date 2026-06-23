### Title
Premature State Mutation Before Network Interaction Causes Silent Propagation Failure for Locally Submitted Transactions - (File: sync/src/relayer/mod.rs)

---

### Summary

In `send_bulk_of_tx_hashes`, the relay state is mutated (`mark_as_known_tx`) **before** the network broadcast (`async_filter_broadcast`) completes. If the broadcast fails, the transaction is permanently recorded as "known" and will never be re-broadcast to the affected peer(s), silently suppressing propagation of locally submitted transactions.

---

### Finding Description

In `sync/src/relayer/mod.rs`, the function `send_bulk_of_tx_hashes` handles relaying verified transaction hashes to connected peers. For transactions submitted via local RPC (`original_peer == None`), the code calls `mark_as_known_tx` **inside the inner peer-iteration loop**, before the actual network send occurs in a separate outer loop:

```
// Inner loop — state mutation happens here, per peer, before any broadcast
None => {
    hashes.push(tx_hash.clone());
    self.shared.state().mark_as_known_tx(tx_hash.clone());  // <== State Change
}

// Outer loop — actual network interaction happens here, after all state is mutated
for (peer, hashes) in selected {
    nc.async_filter_broadcast(...).await  // <== External Interaction
}
``` [1](#0-0) [2](#0-1) 

The `mark_as_known_tx` call marks the transaction globally as "known" in the relay filter. Once marked, the relay logic will not attempt to re-broadcast it. Because `take_relay_tx_verify_results` removes the entries from the queue atomically before this function runs, there is no retry path: if the broadcast fails after the state has been mutated, the transaction is silently dropped from relay. [3](#0-2) 

Note the asymmetry: for **remote**-originated transactions (`Some(peer)`), `mark_as_known_tx` is **not** called before the broadcast — the state mutation only occurs for locally submitted transactions, making this a targeted CEI ordering defect. [4](#0-3) 

---

### Impact Explanation

A locally submitted transaction (via JSON-RPC `send_transaction`) that passes pool verification may fail to propagate to one or more peers if the `async_filter_broadcast` call encounters a transient network error or a peer disconnect. Because the transaction is already marked as known and removed from the relay queue, no retry occurs. The transaction exists in the local mempool but is invisible to the rest of the network, preventing it from being mined unless the submitter resubmits or a peer independently discovers it. This breaks the expected propagation guarantee for RPC-submitted transactions.

---

### Likelihood Explanation

The broadcast path (`async_filter_broadcast`) is a network I/O operation that can fail due to: peer disconnection between the inner loop and the outer loop, a full send buffer, or a transient OS-level socket error. These are ordinary operational conditions, not exotic scenarios. Any RPC caller (including an attacker who controls the submitting node or can influence peer connectivity) can trigger this path. The window between state mutation and broadcast is small but non-zero and grows with the number of connected peers, since the inner loop iterates all peers before the outer loop begins sending.

---

### Recommendation

Move `mark_as_known_tx` to **after** a successful `async_filter_broadcast` call, consistent with the Checks-Effects-Interactions pattern. Specifically:

1. Remove `mark_as_known_tx` from the inner peer-iteration loop.
2. In the outer broadcast loop, call `mark_as_known_tx` only when `async_filter_broadcast` returns `Ok`.

This mirrors the correct ordering already used for remote-originated transactions and ensures the relay filter state is only updated when the interaction has actually succeeded.

---

### Proof of Concept

1. Node A starts with several connected peers.
2. A user submits a transaction via `send_transaction` RPC on Node A.
3. The transaction passes verification and enters `tx_verify_results` as `TxVerificationResult::Ok { original_peer: None, tx_hash }`.
4. `send_bulk_of_tx_hashes` is called (triggered by the relay timer).
5. The inner loop runs: for each connected peer, `mark_as_known_tx(tx_hash)` is called — the tx is now globally marked as known.
6. Before the outer broadcast loop completes, one or more peers disconnect (e.g., due to a network partition or an attacker-controlled peer dropping the connection).
7. `async_filter_broadcast` returns an error for those peers; the error is only logged at `debug` level and silently ignored.
8. The transaction is never re-queued (it was already consumed by `take_relay_tx_verify_results`) and is never re-broadcast (it is already in the known-tx filter).
9. The affected peers never learn of the transaction; it is not included in any block assembled by those peers' miners. [5](#0-4)

### Citations

**File:** sync/src/relayer/mod.rs (L631-707)
```rust
    pub async fn send_bulk_of_tx_hashes(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        const BUFFER_SIZE: usize = 42;

        let connected_peers = nc.full_relay_connected_peers();
        if connected_peers.is_empty() {
            return;
        }

        let tx_verify_results = self
            .shared
            .state()
            .take_relay_tx_verify_results(MAX_RELAY_TXS_NUM_PER_BATCH);
        let mut selected: HashMap<PeerIndex, Vec<Byte32>> = HashMap::default();
        {
            for tx_verify_result in tx_verify_results {
                match tx_verify_result {
                    TxVerificationResult::Ok {
                        original_peer,
                        tx_hash,
                    } => {
                        for target in &connected_peers {
                            match original_peer {
                                Some(peer) => {
                                    // broadcast tx hash to all connected peers except original peer
                                    if peer != *target {
                                        let hashes = selected
                                            .entry(*target)
                                            .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                        hashes.push(tx_hash.clone());
                                    }
                                }
                                None => {
                                    // since this tx is submitted through local rpc, it is assumed to be a new tx for all connected peers
                                    let hashes = selected
                                        .entry(*target)
                                        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                    hashes.push(tx_hash.clone());
                                    self.shared.state().mark_as_known_tx(tx_hash.clone());
                                }
                            }
                        }
                    }
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
                    TxVerificationResult::UnknownParents { peer, parents } => {
                        let tx_hashes: Vec<_> = {
                            let mut tx_filter = self.shared.state().tx_filter();
                            tx_filter.remove_expired();
                            parents
                                .into_iter()
                                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                                .collect()
                        };
                        self.shared.state().add_ask_for_txs(peer, tx_hashes);
                    }
                }
            }
        }
        for (peer, hashes) in selected {
            let content = packed::RelayTransactionHashes::new_builder()
                .tx_hashes(hashes)
                .build();
            let message = packed::RelayMessage::new_builder().set(content).build();

            if let Err(err) = nc
                .async_filter_broadcast(TargetSession::Single(peer), message.as_bytes())
                .await
            {
                debug_target!(
                    crate::LOG_TARGET_RELAY,
                    "relayer send TransactionHashes error: {:?}",
                    err,
                );
            }
        }
    }
```
