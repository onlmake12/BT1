### Title
Relay Transaction Marked as Known Before Async Pool Submission Completes, Preventing Re-request on Service Failure — (`sync/src/relayer/transactions_process.rs`)

### Summary

In `TransactionsProcess::execute()`, incoming relay transactions are permanently marked as "known" in the node's `tx_filter` **before** the async `submit_remote_tx` call completes. If the pool service call fails at the channel/service level (not a pool-level rejection), the transaction hash remains in the filter as "known" but the transaction is never actually admitted to the pool. The node will then refuse to re-request the same transaction from any peer for the duration of the TTL window, analogous to the VETH converter marking a merkle proof as used before the conversion succeeds.

### Finding Description

In `sync/src/relayer/transactions_process.rs`, `TransactionsProcess::execute()` processes incoming `RelayTransactions` P2P messages. After basic validation, at line 76 it calls:

```rust
shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));
```

`mark_as_known_txs` does two things atomically:
1. Removes each tx hash from `unknown_tx_hashes` (the pending-request queue)
2. Inserts each tx hash into `tx_filter` (the TTL-based deduplication filter)

Only **after** this state mutation does the code spawn an async task to actually submit the transactions:

```rust
self.relayer.shared.shared().async_handle().spawn(async move {
    for (tx, declared_cycles) in txs {
        if let Err(e) = tx_pool.submit_remote_tx(tx.clone(), declared_cycles, peer).await {
            error!("submit_tx error {}", e);
        }
    }
});
```

If `submit_remote_tx` returns a service-level `Err` (e.g., the tx-pool service channel is closed or the request cannot be dispatched), the error is only logged. No cleanup of the `tx_filter` entry occurs. The `TxVerificationResult::Reject` path — which does call `remove_from_known_txs` — is only triggered by pool-level rejections that flow through the verification result channel, not by service-level failures.

The consequence: the tx hash is in `tx_filter` as "known" but the transaction is absent from the pool. When any peer subsequently announces the same tx hash via `RelayTransactionHashes`, `TransactionHashesProcess::execute()` filters it out:

```rust
.filter(|tx_hash| !tx_filter.contains(tx_hash))
```

The node will not re-request the transaction for the duration of `FILTER_TTL`.

The same pattern exists in `sync/src/relayer/block_proposal_process.rs` at line 59, where `mark_as_known_tx` is called before `notify_txs_async`.

### Impact Explanation

A node that experiences a transient tx-pool service error while processing a `RelayTransactions` message will silently drop valid transactions from its relay pipeline for the TTL window. The transactions are not in the pool, not re-requested from peers, and not propagated further. This degrades transaction propagation for the affected node. Because the `tx_filter` is in-memory and TTL-bounded, the effect is temporary rather than permanent, placing this at low-to-medium severity.

### Likelihood Explanation

Service-level errors on the tx-pool channel are uncommon under normal operation but can occur during node startup, shutdown, or resource exhaustion. An attacker cannot directly trigger this condition from the network. The impact is self-inflicted by the node and does not affect other nodes. Likelihood is low.

### Recommendation

Move `mark_as_known_txs` to after the async submission completes successfully, or implement a cleanup path that calls `remove_from_known_txs` whenever `submit_remote_tx` returns a service-level `Err`. This mirrors the existing `TxVerificationResult::Reject` cleanup path. Alternatively, decouple the "known" marking from the submission attempt: only mark a tx as known once it has been confirmed accepted by the pool (or confirmed rejected with a permanent rejection reason).

### Proof of Concept

1. A remote peer sends a `RelayTransactions` message containing a valid transaction `T`.
2. `TransactionsProcess::execute()` passes the cycle check and calls `mark_as_known_txs([T.hash()])` at line 76 — `T.hash()` is now in `tx_filter`.
3. The spawned async task calls `submit_remote_tx(T, ...)`, which returns `Err(e)` due to a service-level failure. The error is logged; no `remove_from_known_txs` is called.
4. A second peer announces `T.hash()` via `RelayTransactionHashes`. `TransactionHashesProcess::execute()` checks `tx_filter.contains(T.hash())` → `true` → the hash is filtered out and not added to `unknown_tx_hashes`.
5. The node never requests `T` again until `FILTER_TTL` expires. During this window, `T` is absent from the pool and not relayed. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** sync/src/relayer/transactions_process.rs (L76-93)
```rust
        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });
```

**File:** sync/src/types/mod.rs (L1443-1451)
```rust
    pub fn mark_as_known_txs(&self, hashes: impl Iterator<Item = Byte32> + std::clone::Clone) {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut tx_filter = self.tx_filter.lock();

        for hash in hashes {
            unknown_tx_hashes.remove(&hash);
            tx_filter.insert(hash);
        }
    }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-49)
```rust
        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```

**File:** sync/src/relayer/block_proposal_process.rs (L57-62)
```rust
        for (previously_in, tx) in removes.into_iter().zip(unknown_txs) {
            if previously_in {
                sync_state.mark_as_known_tx(tx.hash());
                asked_txs.push(tx);
            }
        }
```
