### Title
Transactions Marked as Known Before Async Pool Submission Succeeds, Causing Silent Permanent Suppression — (`sync/src/relayer/transactions_process.rs`)

---

### Summary

In `TransactionsProcess::execute`, the relay state flag `mark_as_known_txs` is set **unconditionally** before the async tx-pool submission is spawned. If the spawned task's channel call to `submit_remote_tx` fails (a tx-pool service channel error), the transactions are permanently recorded as "known" in the `tx_filter` and removed from `unknown_tx_hashes`, but are never actually submitted to the pool. The node will never re-request these transactions from any peer, and `remove_from_known_txs` is never called because no `TxVerificationResult` is ever produced. This is a direct structural analog to the ZetaChain M-23 bug: a status flag is set unconditionally before confirming the underlying operation succeeded, permanently blocking recovery.

---

### Finding Description

In `sync/src/relayer/transactions_process.rs`, the `execute` method processes incoming `RelayTransactions` P2P messages:

```rust
// Line 76 — unconditional state mutation
shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

// Lines 80–93 — async spawn; result is ignored at the call site
self.relayer.shared.shared().async_handle().spawn(async move {
    for (tx, declared_cycles) in txs {
        if let Err(e) = tx_pool
            .submit_remote_tx(tx.clone(), declared_cycles, peer)
            .await
        {
            error!("submit_tx error {}", e);   // only logged, no state rollback
        }
    }
});
``` [1](#0-0) 

`mark_as_known_txs` performs two irreversible mutations:

1. Removes each hash from `unknown_tx_hashes` (the re-request queue).
2. Inserts each hash into `tx_filter` (the TTL bloom/set that suppresses future relay). [2](#0-1) 

The only place `remove_from_known_txs` is called is inside `send_bulk_of_tx_hashes`, triggered by a `TxVerificationResult::Reject` message emitted from the tx-pool service after it processes the tx: [3](#0-2) 

If `submit_remote_tx` returns a channel error (`Err(e)`), no `TxVerificationResult` is ever sent, so `remove_from_known_txs` is never invoked. The tx hashes remain in `tx_filter` for the full TTL duration, and the node will silently discard any future relay of the same transactions from any peer.

The same structural flaw exists in `block_proposal_process.rs`, where `mark_as_known_tx` is called before `notify_txs_async`, and a channel error on the latter leaves the tx permanently suppressed: [4](#0-3) 

---

### Impact Explanation

A valid transaction received via the relay protocol can be permanently silenced on the affected node for the duration of the `tx_filter` TTL:

- The node will not re-request the transaction from any peer.
- The transaction will not enter the local tx-pool.
- The node will not relay the transaction to its downstream peers.

For a well-connected node (e.g., a mining pool relay node or a high-connectivity full node), this suppresses propagation of the affected transactions across a significant portion of the network graph, delaying or preventing confirmation. This is a transaction-censorship / availability impact, not a direct loss of on-chain funds, but it degrades the liveness guarantee of the relay protocol.

---

### Likelihood Explanation

The trigger condition is a tx-pool service channel error during `submit_remote_tx`. This can occur:

- Under sustained high load when the tx-pool's internal channel is saturated.
- During node startup/shutdown races when the tx-pool service is not yet ready.
- If an attacker floods the tx-pool submission channel with a large volume of transactions, causing back-pressure that causes channel sends to fail for legitimate transactions arriving concurrently via relay.

An unprivileged peer can send `RelayTransactions` messages (the standard relay protocol path) and, combined with concurrent flooding, trigger the condition without any privileged access. The attacker entry path is fully externally reachable.

---

### Recommendation

Move `mark_as_known_txs` to **after** confirmed successful submission, or implement a rollback: if `submit_remote_tx` returns a channel error, call `remove_from_known_txs` for the affected tx hash so the node can re-request it from peers. Concretely, inside the spawned async block:

```rust
for (tx, declared_cycles) in txs {
    if let Err(e) = tx_pool
        .submit_remote_tx(tx.clone(), declared_cycles, peer)
        .await
    {
        error!("submit_tx error {}", e);
        // Rollback: allow re-request from peers
        shared_state_clone.remove_from_known_txs(&tx.hash());
    }
}
```

Alternatively, defer `mark_as_known_txs` until the tx-pool service confirms receipt (i.e., move it into the async block, after a successful `submit_remote_tx` call).

---

### Proof of Concept

1. Peer A sends a `RelayTransactions` message containing a valid transaction `T` to the target node.
2. Concurrently, an attacker (or high load) saturates the tx-pool service channel, causing `submit_remote_tx` to return `Err(channel_error)`.
3. `mark_as_known_txs([T.hash()])` has already executed at line 76 — `T` is now in `tx_filter` and removed from `unknown_tx_hashes`.
4. The error is logged; no `TxVerificationResult` is emitted; `remove_from_known_txs` is never called.
5. Peer B later announces `T` via `RelayTransactionHashes`. The node checks `tx_filter`, finds `T` already "known", and does not add it to `unknown_tx_hashes` — no re-request is issued.
6. `T` never enters the local tx-pool and is never relayed further, for the full TTL duration of `tx_filter`. [5](#0-4) [6](#0-5)

### Citations

**File:** sync/src/relayer/transactions_process.rs (L37-96)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };

        if txs.is_empty() {
            return Status::ok();
        }

        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }

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

        Status::ok()
    }
```

**File:** sync/src/types/mod.rs (L1432-1451)
```rust
    pub fn mark_as_known_tx(&self, hash: Byte32) {
        self.mark_as_known_txs(iter::once(hash));
    }

    pub fn remove_from_known_txs(&self, hash: &Byte32) {
        self.tx_filter.lock().remove(hash);
    }

    // maybe someday we can use
    // where T: Iterator<Item=Byte32>,
    // for<'a> &'a T: Iterator<Item=&'a Byte32>,
    pub fn mark_as_known_txs(&self, hashes: impl Iterator<Item = Byte32> + std::clone::Clone) {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut tx_filter = self.tx_filter.lock();

        for hash in hashes {
            unknown_tx_hashes.remove(&hash);
            tx_filter.insert(hash);
        }
    }
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```

**File:** sync/src/relayer/block_proposal_process.rs (L57-75)
```rust
        for (previously_in, tx) in removes.into_iter().zip(unknown_txs) {
            if previously_in {
                sync_state.mark_as_known_tx(tx.hash());
                asked_txs.push(tx);
            }
        }

        if asked_txs.is_empty() {
            return Status::ignored();
        }

        let tx_pool = self.relayer.shared.shared().tx_pool_controller();
        if let Err(err) = tx_pool.notify_txs_async(asked_txs).await {
            warn_target!(
                crate::LOG_TARGET_RELAY,
                "BlockProposal notify_txs error: {:?}",
                err,
            );
        }
```
