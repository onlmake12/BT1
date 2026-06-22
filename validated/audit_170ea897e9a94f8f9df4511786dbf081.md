### Title
Orphan Tx-Pool Griefing via No Per-Peer Limit Allows Attacker to Evict Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

---

### Summary

The `OrphanPool` in CKB's tx-pool enforces a global cap of 100 entries (`DEFAULT_MAX_ORPHAN_TRANSACTIONS`) with random eviction and no per-peer submission limit. A malicious relay peer can flood the pool with 100 fake orphan transactions referencing non-existent parent outputs, filling the entire pool and causing random eviction of legitimate orphan transactions from honest users. Evicted orphans are immediately marked as "rejected" in the relay bloom filter, preventing automatic re-fetching, and causing honest users' transactions to be silently dropped from the node's orphan pool without any notification.

---

### Finding Description

**Root cause — `tx-pool/src/component/orphan.rs`:**

The `OrphanPool` struct stores orphan transactions in a flat `HashMap<ProposalShortId, Entry>` with no per-peer accounting:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

The global cap is `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. When the pool is full, `limit_size()` evicts entries **randomly** using HashMap iteration order:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

There is no per-peer quota, no priority based on fee rate, and no protection for orphans from honest peers. Any relay peer can call `add_orphan_tx` up to 100 times with distinct transactions referencing non-existent parent outputs (each transaction is unique by `proposal_short_id`), filling the entire pool.

**Eviction consequence — `tx-pool/src/process.rs`:**

When a legitimate orphan is evicted, `add_orphan` sends a `TxVerificationResult::Reject` for each evicted hash:

```rust
for tx_hash in evicted_txs {
    self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
}
```

This causes the relay layer to mark the evicted transaction as "rejected/unknown" in its bloom filter. The node will not re-request it from peers. The honest user's orphan transaction is silently lost from this node's perspective.

**Attack path:**

1. Attacker connects as a relay peer (no privilege required).
2. Attacker sends 100 relay messages, each containing a distinct orphan transaction whose inputs reference a fabricated (non-existent) out-point. Each passes the `is_missing_input` check and is admitted to the orphan pool via `add_orphan`.
3. The orphan pool reaches `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. Any subsequent legitimate orphan from an honest user triggers random eviction of one of the 100 slots.
4. The evicted legitimate orphan is marked as rejected in the relay filter. When the legitimate parent transaction is later confirmed, `process_orphan_tx` performs a BFS over orphans referencing that parent — but the legitimate child is no longer present, so it is never promoted to pending.
5. The attacker's fake orphans persist for `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` seconds (approximately 100 × 30s = ~50 minutes), continuously occupying all 100 slots.
6. The attacker can refresh the fake orphans before expiry to maintain the griefing indefinitely.

---

### Impact Explanation

Legitimate users' orphan transactions are evicted and permanently marked as rejected in the relay filter. The node will not automatically re-fetch or re-process them when their parent is confirmed. The user's transaction is silently dropped and must be manually re-submitted. In a high-throughput scenario or during network congestion (when orphan transactions are common), this disrupts normal transaction flow for all honest users connected to the attacked node. The attacker can sustain this indefinitely at negligible cost (100 small transactions, refreshed every ~50 minutes).

---

### Likelihood Explanation

Any unprivileged peer connected via the relay protocol can execute this attack. No keys, funds, or special access are required beyond a standard P2P connection. The cost is 100 minimal-size transactions with fabricated inputs (no on-chain funds needed since orphan admission does not verify input existence). The attack is trivially automatable and can be sustained indefinitely.

---

### Recommendation

1. **Add a per-peer orphan limit**: Track how many orphan transactions each `PeerIndex` has contributed. Reject or preferentially evict orphans from peers that have already contributed a disproportionate share (e.g., cap at `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`).
2. **Prefer evicting orphans from the most-contributing peer**: Replace the random eviction in `limit_size()` with a policy that evicts from the peer with the most entries in the pool, making flooding self-defeating.
3. **Apply a minimum fee-rate check before orphan admission**: Orphans that carry no fee (or below `min_fee_rate`) are cheap to fabricate and should be rejected or deprioritized.

---

### Proof of Concept

```
1. Attacker connects to a CKB node as a relay peer.
2. Attacker constructs 100 transactions T_1..T_100, each spending a distinct
   fabricated out-point (tx_hash=random, index=0). Each transaction has a
   valid structure but references a non-existent parent.
3. Attacker sends RelayTransactionHashes for T_1..T_100.
   Node requests them; attacker sends them via RelayTransactions.
   Each is rejected with is_missing_input=true → added to OrphanPool.
   After 100 submissions, OrphanPool.len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS.
4. Honest user U submits child transaction C (whose parent P is not yet in pool).
   C is also missing input → add_orphan_tx is called → limit_size() triggers
   random eviction of one of T_1..T_100 OR of C itself.
   If C is evicted: send_result_to_relayer(Reject{C.hash}) → C marked unknown.
5. Parent P is confirmed in a block. process_orphan_tx(P) is called.
   find_by_previous(P) returns empty (C was evicted). C is never promoted.
6. U's transaction C is silently lost. U must manually re-submit.
7. Attacker refreshes T_1..T_100 before ORPHAN_TX_EXPIRE_TIME to sustain attack.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L96-132)
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
    }
```

**File:** tx-pool/src/component/orphan.rs (L134-159)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
    }
```

**File:** tx-pool/src/process.rs (L557-572)
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
```

**File:** tx-pool/src/process.rs (L591-641)
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
```
