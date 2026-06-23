### Title
Premature `mark_as_known_txs` Before Async Pool Submission Verification — (`sync/src/relayer/transactions_process.rs`)

### Summary

In `TransactionsProcess::execute()`, the relay state is updated to mark incoming transactions as "known" **before** the asynchronous pool submission result is verified. If the pool submission fails (channel error), no cleanup is performed, leaving the transactions permanently suppressed in the relay filter until TTL expiry. This is the direct CKB analog of the Ethereum bridge's pattern of updating state (emitting `Withdrawal` / setting `used[txHash]`) before checking the return value of the critical operation.

### Finding Description

In `sync/src/relayer/transactions_process.rs`, `TransactionsProcess::execute()` processes incoming `RelayTransactions` P2P messages from remote peers: [1](#0-0) 

At line 76, `shared_state.mark_as_known_txs(...)` is called unconditionally — inserting all transaction hashes into the `tx_filter` (a TTL bloom filter) — **before** the async `submit_remote_tx` call is dispatched and **before** its result is known: [2](#0-1) 

The actual pool submission is spawned asynchronously: [3](#0-2) 

If `submit_remote_tx` returns a channel error (line 86–91), only `error!` is logged. No call to `remove_from_known_txs` is made. The `after_process` path — which does call `remove_from_known_txs` via `TxVerificationResult::Reject` — is never reached for channel-level failures: [4](#0-3) 

The `mark_as_known_txs` function inserts into `tx_filter` and removes from `unknown_tx_hashes`: [5](#0-4) 

The `remove_from_known_txs` cleanup that would undo this only happens via the relay verification result channel: [6](#0-5) 

### Impact Explanation

A transaction hash inserted into `tx_filter` causes the node to treat that transaction as "already known." Any subsequent `RelayTransactions` message from any peer carrying the same tx hash is silently dropped by the filter check at line 49–55: [7](#0-6) 

If the pool submission channel fails after `mark_as_known_txs` has already run, the transaction is suppressed from relay propagation through this node until the TTL expires. A valid transaction that was never actually submitted to the pool is treated as if it were already processed. This is the CKB analog of the Ethereum bridge emitting `Withdrawal` without confirming the transfer succeeded: **relay state is updated as if the operation succeeded before the operation's result is verified**.

### Likelihood Explanation

The tx_pool controller channel (`submit_remote_tx`) can fail if the `TxPoolService` is overloaded or its internal channel is full. This is reachable by an unprivileged relay peer: a peer sending a large batch of transactions that saturates the verify queue can induce channel backpressure. The entry path is the standard `RelayTransactions` P2P message, reachable by any connected peer. [8](#0-7) 

### Recommendation

Move `mark_as_known_txs` to **after** the async submission result is confirmed (i.e., inside the spawned async block, only on success), or add a `remove_from_known_txs` call in the error branch at lines 86–91 to undo the premature state update when the channel send fails:

```rust
// In the error branch:
Err(e) => {
    error!("submit_tx error {}", e);
    shared_state.remove_from_known_txs(&tx.hash()); // undo premature mark
}
```

### Proof of Concept

1. Connect to a CKB node as a relay peer.
2. Send a `RelayTransactions` message containing a valid transaction hash that the node has previously requested (so it passes the `unknown_tx_hashes` filter).
3. Simultaneously saturate the tx_pool verify queue to cause the `submit_remote_tx` channel to return an error.
4. Observe: `mark_as_known_txs` has already run (line 76), the tx hash is in `tx_filter`, but the tx was never submitted to the pool.
5. Send the same transaction again from a different peer: it is silently dropped by the `tx_filter.contains` check at line 50, even though it was never processed. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/process.rs (L458-555)
```rust
    pub(crate) async fn after_process(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
        _snapshot: &Snapshot,
        ret: &Result<Completed, Reject>,
    ) {
        let tx_hash = tx.hash();

        // log tx verification result for monitor node
        if log_enabled_target!("ckb_tx_monitor", Trace)
            && let Ok(c) = ret
        {
            trace_target!(
                "ckb_tx_monitor",
                r#"{{"tx_hash":"{:#x}","cycles":{}}}"#,
                tx_hash,
                c.cycles
            );
        }

        if matches!(
            ret,
            Err(Reject::RBFRejected(..) | Reject::Resolve(OutPointError::Dead(_)))
        ) {
            let mut tx_pool = self.tx_pool.write().await;
            if tx_pool.pool_map.find_conflict_outpoint(&tx).is_some() {
                tx_pool.record_conflict(tx.clone());
            }
        }

        match remote {
            Some((declared_cycle, peer)) => match ret {
                Ok(_) => {
                    debug!(
                        "after_process remote send_result_to_relayer {} {}",
                        tx_hash, peer
                    );
                    self.send_result_to_relayer(TxVerificationResult::Ok {
                        original_peer: Some(peer),
                        tx_hash,
                    });
                    self.process_orphan_tx(&tx).await;
                }
                Err(reject) => {
                    debug!(
                        "after_process {} {} remote reject: {} ",
                        tx_hash, peer, reject
                    );
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
                }
            },
            None => {
                match ret {
                    Ok(_) => {
                        debug!("after_process local send_result_to_relayer {}", tx_hash);
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
                        self.process_orphan_tx(&tx).await;
                    }
                    Err(Reject::Duplicated(_)) => {
                        debug!("after_process {} duplicated", tx_hash);
                        // re-broadcast tx when it's duplicated and submitted through local rpc
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
                    }
                    Err(reject) => {
                        debug!("after_process {} reject: {} ", tx_hash, reject);
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
                }
            }
        }
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
