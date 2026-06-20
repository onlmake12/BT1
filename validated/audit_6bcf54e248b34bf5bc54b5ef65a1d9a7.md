### Title
Orphan Transaction Pool Random Eviction Enables Attacker to Permanently Discard Legitimate Out-of-Order Transactions — (File: `tx-pool/src/component/orphan.rs`)

---

### Summary

CKB's orphan transaction pool (`OrphanPool`) is capped at 100 entries and evicts entries **randomly** (no per-peer accounting) when full. An unprivileged P2P peer can flood the pool with 100 synthetic orphan transactions, causing legitimate out-of-order child transactions to be silently evicted. When the legitimate parent later arrives, `process_orphan_tx` finds no child in the pool and the child transaction is permanently lost from the node — a direct analog to the ordering/sequencing failure described in the external report.

---

### Finding Description

The external report describes a two-step cross-chain operation where two messages travel via independent channels with no delivery-order guarantee. When the second message (the instruction) arrives before the first (the tokens), the instruction fails. The fix was to cache failed messages so they can be retried.

The CKB analog is structurally identical: a child transaction that arrives before its parent is placed in the `OrphanPool` as a "cached pending" entry, waiting for the parent to arrive. When the parent arrives, `process_orphan_tx` drains the cache and promotes the child. However, the cache (orphan pool) has a hard cap of 100 entries with no per-peer isolation, and eviction is random. An attacker who fills the pool with junk orphans causes legitimate cached children to be silently discarded — permanently breaking the ordering-recovery mechanism.

**Root cause — `OrphanPool::limit_size`:** [1](#0-0) 

The pool is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`: [2](#0-1) 

When the cap is exceeded, one entry is evicted by calling `self.entries.keys().next()` on a `HashMap` — effectively arbitrary, with no preference for the most-recently-added or attacker-supplied entry: [3](#0-2) 

There is no per-peer quota. A single peer can contribute all 100 slots. The eviction path notifies the relayer layer to mark the evicted tx as `Reject` (unknown), but does **not** re-request it from the network: [4](#0-3) 

When the legitimate parent later arrives, `process_orphan_tx` performs a BFS over `by_out_point` to find children: [5](#0-4) 

If the child was evicted, `find_by_previous` returns nothing and the child is permanently gone from this node. [6](#0-5) 

The `after_process` path that adds to the orphan pool is reachable by any P2P peer sending a `RelayTransactions` message whose transactions fail with `is_missing_input`: [7](#0-6) 

---

### Impact Explanation

A legitimate user submits a child transaction (e.g., spending an output of an unconfirmed parent) via P2P relay. The child arrives at a victim node before the parent and enters the orphan pool. An attacker who has already filled the pool with 100 junk orphans (each referencing a non-existent parent) causes the legitimate child to be immediately evicted upon insertion. When the legitimate parent arrives, `process_orphan_tx` finds no child and does nothing. The child transaction is silently lost from the victim node's mempool. The user's transaction chain stalls on that node; the user must detect the failure and resubmit. In a network of many nodes all under the same attack, the child may be lost across the entire reachable peer set, causing effective transaction censorship without any on-chain action.

---

### Likelihood Explanation

The attack requires only a standard P2P connection (no keys, no stake). The attacker sends 100 `RelayTransactions` messages each containing one orphan transaction referencing a fabricated non-existent parent `OutPoint`. The rate limiter in `Relayer` allows 30 messages/second per peer: [8](#0-7) 

So the pool can be saturated in under 4 seconds from a single peer. The attack is cheap, repeatable, and requires no privileged access. The orphan pool is global (not per-peer), so one attacker connection affects all users of the node.

---

### Recommendation

1. **Per-peer orphan quota**: Track how many orphan slots each peer occupies. When eviction is needed, prefer evicting entries from the peer with the most slots (or the attacker peer specifically).
2. **Evict the newest entry, not a random one**: When the pool is full and a new entry must be added, evict the entry that was just added (i.e., reject the new submission) rather than a random existing entry. This prevents an attacker from displacing already-cached legitimate entries.
3. **Re-request evicted orphans**: When an orphan is evicted due to pool pressure (not expiry), send a `GetRelayTransactions` to re-fetch it if the parent later arrives, rather than silently discarding it.

---

### Proof of Concept

1. Attacker connects to a CKB node as a P2P peer using `SupportProtocols::RelayV3`.
2. Attacker generates 100 transactions each spending a fabricated `OutPoint` (random tx hash, index 0) that does not exist on-chain or in the pool. Each transaction is otherwise structurally valid (correct capacity, lock script, etc.).
3. Attacker sends each as a `RelayTransactions` message. Each is rejected with `OutPointError::Unknown`, triggering `is_missing_input → add_orphan`. After 100 submissions, the orphan pool is full.
4. A legitimate user's child transaction (spending an unconfirmed parent) arrives at the same node via relay. It is added to the orphan pool (size becomes 101), triggering `limit_size`, which randomly evicts one entry — potentially the legitimate child itself.
5. The legitimate parent transaction arrives. `process_orphan_tx` calls `find_by_previous` which returns empty (child was evicted). The child is permanently lost from this node's mempool.
6. Confirmed by inspecting `OrphanPool::add_orphan_tx` → `limit_size` → random eviction at line 121 of `tx-pool/src/component/orphan.rs`, and `process_orphan_tx` BFS at line 596 of `tx-pool/src/process.rs` which silently does nothing when the child is absent. [9](#0-8)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
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

**File:** tx-pool/src/component/orphan.rs (L161-167)
```rust
    pub fn find_by_previous(&self, tx: &TransactionView) -> Vec<&ProposalShortId> {
        tx.output_pts()
            .iter()
            .filter_map(|out_point| self.by_out_point.get(out_point))
            .flatten()
            .collect::<Vec<_>>()
    }
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
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

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
