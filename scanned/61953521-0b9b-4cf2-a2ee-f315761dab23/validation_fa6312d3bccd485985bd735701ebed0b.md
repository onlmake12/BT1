### Title
`ban_malformed` Does Not Remove Banned Peer's Transactions from Orphan Pool - (`tx-pool/src/process.rs`)

---

### Summary

When `ban_malformed` is called to ban a peer for submitting a malformed transaction, it removes that peer's transactions from the `verify_queue` but does **not** remove them from the `orphan` pool. This is a direct analog to M-30: a "blacklist/ban" operation updates the ban state without fully draining all associated resources, leaving the peer's orphan transactions stuck in the pool and eligible for future processing.

---

### Finding Description

In `tx-pool/src/process.rs`, `ban_malformed` performs two actions:

1. Bans the peer at the network layer via `self.network.ban_peer(...)`.
2. Removes the peer's pending transactions from the `verify_queue` via `self.verify_queue.write().await.remove_txs_by_peer(&peer)`. [1](#0-0) 

However, it never touches the `orphan` pool. The `OrphanPool` stores the submitting peer's `PeerIndex` in each `Entry` (confirmed by the `add_orphan_tx(tx, peer, declared_cycle)` call and the `orphan.peer` field accessed during `process_orphan_tx`): [2](#0-1) [3](#0-2) 

The `VerifyQueue` has `remove_txs_by_peer`: [4](#0-3) 

But `OrphanPool` has no equivalent method — only `remove_orphan_tx` (single entry by ID) and `remove_orphan_txs` (batch by ID): [5](#0-4) 

So after `ban_malformed` completes, the banned peer's orphan transactions remain in the pool indefinitely (until the orphan pool's own size-based eviction runs).

---

### Impact Explanation

**Incomplete ban enforcement**: A banned peer's orphan transactions remain eligible for processing. When any parent transaction for those orphans later appears on-chain or in the pool, `process_orphan_tx` will pick them up and attempt to submit them to the main pool: [6](#0-5) 

This means the ban does not fully prevent the peer's transaction pipeline from completing — the orphan transactions bypass the ban and can still be committed to the chain if valid.

**Resource exhaustion**: The orphan pool has a fixed capacity (`DEFAULT_MAX_ORPHAN_TRANSACTIONS`). A malicious peer can pre-fill the orphan pool with child transactions, trigger `ban_malformed` (which only clears the verify_queue), and leave the orphan pool slots occupied. Legitimate orphan transactions from honest peers are then randomly evicted to make room: [7](#0-6) 

---

### Likelihood Explanation

The attack path is fully reachable by any unprivileged remote peer:

1. Peer connects and relays several child transactions (whose parents are unknown) — these land in the orphan pool with the peer's `PeerIndex` recorded.
2. Peer then relays a transaction that fails `is_malformed_tx()` (e.g., wrong declared cycles, oversized, or script-level malformation).
3. `after_process` detects the malformed rejection and calls `ban_malformed`: [8](#0-7) 

4. The peer is banned and removed from the verify_queue, but the orphan pool is untouched.

No privileged access, key material, or majority hashpower is required.

---

### Recommendation

Add peer-based cleanup of the orphan pool inside `ban_malformed`, mirroring the existing `remove_txs_by_peer` pattern used for the verify_queue:

```rust
async fn ban_malformed(&self, peer: PeerIndex, reason: String) {
    const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);
    // ... sentry reporting ...
    self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
    self.verify_queue.write().await.remove_txs_by_peer(&peer);
    // Add: remove the peer's orphan transactions as well
    let ids: Vec<_> = {
        let orphan = self.orphan.read().await;
        orphan.entries.iter()
            .filter(|(_, e)| e.peer == peer)
            .map(|(id, _)| id.clone())
            .collect()
    };
    self.orphan.write().await.remove_orphan_txs(ids.into_iter());
}
```

Additionally, `OrphanPool` should expose a `remove_txs_by_peer` method analogous to `VerifyQueue::remove_txs_by_peer` for symmetry and future use.

---

### Proof of Concept

1. Attacker peer connects to a CKB node.
2. Attacker sends `N` child transactions (spending unknown parent outputs) via the relay protocol — each lands in `OrphanPool` with the attacker's `PeerIndex`.
3. Attacker sends one transaction with a deliberately wrong declared cycle count, triggering `Reject::DeclaredWrongCycles`, which satisfies `is_malformed_tx()`.
4. `ban_malformed` fires: attacker is banned at the network layer; verify_queue is cleared of attacker's entries.
5. Inspection of the orphan pool shows all `N` attacker transactions still present.
6. A separate honest peer (or on-chain event) provides the parent transaction; `process_orphan_tx` resolves and submits the attacker's orphan transactions to the main pool — despite the attacker being banned. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/process.rs (L513-516)
```rust
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
```

**File:** tx-pool/src/process.rs (L557-573)
```rust
    pub(crate) async fn add_orphan(
        &self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) {
        let evicted_txs = self
            .orphan
            .write()
            .await
            .add_orphan_tx(tx, peer, declared_cycle);
        // for any evicted orphan tx, we should send reject to relayer
        // so that we mark it as `unknown` in filter
        for tx_hash in evicted_txs {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }
    }
```

**File:** tx-pool/src/process.rs (L591-670)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
            for orphan in orphans.into_iter() {
                if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
                    debug!(
                        "process_orphan {} added to verify queue; find previous from {}",
                        orphan.tx.hash(),
                        tx.hash(),
                    );
                    let orphan_id = orphan.tx.proposal_short_id();
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
                        }
                        Err(reject) => {
                            warn!(
                                "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );
                        }
                    }
                } else if let Some((ret, _snapshot)) = self
                    ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
                    .await
                {
                    match ret {
                        Ok(_) => {
                            self.send_result_to_relayer(TxVerificationResult::Ok {
                                original_peer: Some(orphan.peer),
                                tx_hash: orphan.tx.hash(),
                            });
                            debug!(
                                "process_orphan {} success, find previous from {}",
                                orphan.tx.hash(),
                                tx.hash()
                            );
                            self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                            orphan_queue.push_back(orphan.tx);
                        }
                        Err(reject) => {
                            debug!(
                                "process_orphan {} reject {}, find previous from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );

                            if !is_missing_input(&reject) {
                                self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                                if reject.is_malformed_tx() {
                                    self.ban_malformed(orphan.peer, format!("reject {reject}"))
                                        .await;
                                }
                                if reject.is_allowed_relay() {
                                    self.send_result_to_relayer(TxVerificationResult::Reject {
                                        tx_hash: orphan.tx.hash(),
                                    });
                                }
                                if reject.should_recorded() {
                                    self.put_recent_reject(&orphan.tx.hash(), &reject).await;
                                }
                            }
                        }
                    }
                }
            }
        }
```

**File:** tx-pool/src/process.rs (L679-703)
```rust
    async fn ban_malformed(&self, peer: PeerIndex, reason: String) {
        const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);

        #[cfg(feature = "with_sentry")]
        use sentry::{Level, capture_message, with_scope};

        #[cfg(feature = "with_sentry")]
        with_scope(
            |scope| scope.set_fingerprint(Some(&["ckb-tx-pool", "receive-invalid-remote-tx"])),
            || {
                capture_message(
                    &format!(
                        "Ban peer {} for {} seconds, reason: \
                        {}",
                        peer,
                        DEFAULT_BAN_TIME.as_secs(),
                        reason
                    ),
                    Level::Info,
                )
            },
        );
        self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
        self.verify_queue.write().await.remove_txs_by_peer(&peer);
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L158-168)
```rust
    /// Remove multiple txs from the queue from a specified peer
    pub fn remove_txs_by_peer(&mut self, peer: &PeerIndex) {
        let ids: Vec<_> = self
            .inner
            .iter()
            .filter(|&(_cycle, entry)| entry.inner.remote.as_ref().is_some_and(|(_, p)| p == peer))
            .map(|(_cycle, entry)| entry.id.clone())
            .collect();

        self.remove_txs(ids.into_iter());
    }
```

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L74-94)
```rust
    pub fn remove_orphan_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
        self.entries.remove(id).inspect(|entry| {
            debug!("remove orphan tx {}", entry.tx.hash());
            for out_point in entry.tx.input_pts_iter() {
                if let Some(ids_set) = self.by_out_point.get_mut(&out_point) {
                    ids_set.remove(id);

                    if ids_set.is_empty() {
                        self.by_out_point.remove(&out_point);
                    }
                }
            }
        })
    }

    pub fn remove_orphan_txs(&mut self, ids: impl Iterator<Item = ProposalShortId>) {
        for id in ids {
            self.remove_orphan_tx(&id);
        }
        self.shrink_to_fit();
    }
```

**File:** tx-pool/src/component/orphan.rs (L96-131)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
```
