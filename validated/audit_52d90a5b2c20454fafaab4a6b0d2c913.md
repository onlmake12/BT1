### Title
State Marked as Known Before Async Tx Pool Submission Completes — (`File: sync/src/relayer/transactions_process.rs`)

---

### Summary

In `TransactionsProcess::execute()`, the relay state (`tx_filter`) is updated to mark transactions as "known" **before** the asynchronous tx-pool submission completes. If the tx-pool rejects the transaction with a non-relay-allowed error, or if the submission channel fails, `remove_from_known_txs` is never called. The transaction remains permanently suppressed in the node's relay filter for the TTL duration, and any subsequent relay of the same transaction from honest peers is silently dropped.

---

### Finding Description

In `sync/src/relayer/transactions_process.rs`, the `execute()` function follows this sequence:

1. **Check** (lines 39–57): Filter incoming transactions — only accept those not already in `tx_filter` and whose hash was specifically requested from this peer.
2. **Check** (lines 63–74): Validate declared cycles against `max_block_cycles`.
3. **Effect — state modification** (line 76): `shared_state.mark_as_known_txs(...)` — immediately inserts every tx hash into `tx_filter` and removes it from `unknown_tx_hashes`.
4. **Interaction** (lines 78–93): Spawn an async task that calls `tx_pool.submit_remote_tx(...)`. [1](#0-0) 

`mark_as_known_txs` does two things atomically:

- Removes the hash from `unknown_tx_hashes` (preventing re-request from any peer).
- Inserts the hash into `tx_filter` (causing all future relay messages containing this hash to be silently ignored). [2](#0-1) 

The only cleanup path is in `send_bulk_of_tx_hashes`, which calls `remove_from_known_txs` only when a `TxVerificationResult::Reject` message is received through the relay sender channel: [3](#0-2) 

That `TxVerificationResult::Reject` is sent from `after_process` only when `reject.is_allowed_relay()` is true: [4](#0-3) 

Rejections such as `Reject::Full`, `Reject::Duplicated`, and `Reject::Expiry` do **not** satisfy `is_allowed_relay()`, so `remove_from_known_txs` is never called for them. Similarly, if `submit_remote_tx` returns a channel-level `Err(e)` (line 86–91), the error is only logged — no cleanup occurs. [5](#0-4) 

---

### Impact Explanation

A malicious peer can permanently suppress a specific transaction from being accepted by the victim node for the full TTL duration of `tx_filter`:

1. The attacker announces a target tx hash `H` via `RelayTransactionHashes`.
2. The victim node adds `H` to `unknown_tx_hashes` and eventually selects the attacker as the requesting peer.
3. The attacker sends the actual tx body for `H` with a manipulated `declared_cycles` value that is ≤ `max_block_cycles` (passes the initial check) but does not match the actual execution cycles.
4. Line 76 fires: `H` is inserted into `tx_filter` and removed from `unknown_tx_hashes`.
5. The async spawn submits to the tx pool; the pool rejects with `Reject::DeclaredWrongCycles` or another non-relay-allowed error.
6. `remove_from_known_txs` is not called.
7. `H` remains in `tx_filter`. All subsequent `RelayTransactions` messages from honest peers containing `H` are silently dropped at line 50.

The victim node is effectively censored from receiving transaction `H` until the TTL expires. This is reachable by any unprivileged P2P peer that can participate in the relay protocol.

---

### Likelihood Explanation

**Medium.** Any peer connected to the victim node can announce arbitrary tx hashes. The attacker only needs to be selected as the requesting peer for a given hash — a condition that is met whenever the attacker is the first (or only) peer to announce the hash. No special privileges, keys, or majority hashpower are required. The attack is repeatable and can target specific high-value transactions (e.g., time-sensitive DeFi transactions or specific user transactions).

---

### Recommendation

Move `mark_as_known_txs` to **after** the tx-pool submission result is known, inside the async spawn, and only call it on success. On any failure path (including non-relay-allowed rejections and channel errors), ensure `remove_from_known_txs` is called unconditionally so the hash is cleared from `tx_filter`.

Concretely, the fix pattern should be:

```
// BEFORE (CEI violation):
shared_state.mark_as_known_txs(...);   // Effect
spawn(async { tx_pool.submit_remote_tx(...).await; }); // Interaction

// AFTER (CEI-compliant):
spawn(async {
    match tx_pool.submit_remote_tx(...).await {
        Ok(_)  => shared_state.mark_as_known_txs(...),
        Err(_) => shared_state.remove_from_known_txs(...),
    }
});
```

---

### Proof of Concept

**Setup**: Attacker peer `P` is connected to victim node `V`. A valid transaction `T` with hash `H` exists in the network.

1. `P` sends a `RelayTransactionHashes` message to `V` containing `H`.
2. `V` adds `H` to `unknown_tx_hashes` with `P` as the requesting peer.
3. `V` sends `GetRelayTransactions { tx_hashes: [H] }` to `P`.
4. `P` responds with `RelayTransactions` containing the body of `T` but with `declared_cycles` set to `actual_cycles - 1` (a value ≤ `max_block_cycles`, passing line 66).
5. `TransactionsProcess::execute()` runs:
   - Line 76: `mark_as_known_txs([H])` — `H` enters `tx_filter`, removed from `unknown_tx_hashes`.
   - Lines 80–93: async spawn calls `submit_remote_tx(T, declared_cycles - 1, P)`.
6. Tx pool rejects with `Reject::DeclaredWrongCycles(declared, actual)`.
7. `after_process` checks `reject.is_allowed_relay()` — for `DeclaredWrongCycles` this may or may not be true; if false, `remove_from_known_txs` is never called.
8. Honest peer `Q` sends `RelayTransactions` containing `T` with correct cycles.
9. `TransactionsProcess::execute()` on `Q`'s message: line 50 checks `tx_filter.contains(&H)` → **true** → `T` is filtered out and never submitted to the tx pool.
10. `V` never receives `T` for the TTL duration. [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** sync/src/relayer/mod.rs (L639-675)
```rust
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
```

**File:** tx-pool/src/process.rs (L458-525)
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
```
