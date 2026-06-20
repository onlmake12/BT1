### Title
Orphan Transaction Pool Evicts Legitimate Transactions via Random Eviction Without Per-Peer Fairness — (`File: tx-pool/src/component/orphan.rs`)

---

### Summary

The `OrphanPool` in CKB's tx-pool evicts entries **randomly** (using `HashMap::keys().next()`) when the pool reaches its hard cap of 100 entries (`DEFAULT_MAX_ORPHAN_TRANSACTIONS`). There is no per-peer quota, no fee-rate priority, and no protection for legitimate orphan transactions. An unprivileged P2P relay peer can flood the orphan pool with 100 cheap junk orphan transactions, causing legitimate users' orphan transactions to be evicted with high probability. When the parent transaction later arrives, `process_orphan_tx` finds no children in the orphan pool and the transaction chain silently breaks — the child transaction is permanently lost from the node's view until the user resubmits it.

This is a direct analog to the PSM3 DoS: a shared bounded resource (orphan pool slots instead of asset balance) is depleted by a malicious actor, preventing legitimate users from completing their intended operation (transaction chain propagation instead of asset withdrawal).

---

### Finding Description

`OrphanPool` is a fixed-size in-memory store for transactions whose inputs are not yet known to the node. When a relayed transaction fails resolution with a missing-input error, `after_process` calls `add_orphan` → `add_orphan_tx` → `limit_size`. [1](#0-0) 

The cap is 100 entries. When exceeded, `limit_size` first removes expired entries, then evicts randomly: [2](#0-1) 

`HashMap::keys().next()` in Rust returns an arbitrary key determined by the internal hash table layout (randomized per-process by `HashDoS` protection). There is no ordering by fee rate, arrival time, or peer identity. The `Entry` struct stores `peer: PeerIndex` but it is never consulted during eviction: [3](#0-2) 

The attacker entry path is the P2P relay protocol. Any connected peer can send `RelayTransaction` messages. When a transaction's inputs are missing, `after_process` unconditionally calls `add_orphan`: [4](#0-3) 

There is no per-peer rate limit on orphan submissions anywhere in `add_orphan_tx` or `limit_size`. [5](#0-4) 

When the parent transaction later arrives and is accepted, `process_orphan_tx` uses `find_by_previous` to look up children in the orphan pool: [6](#0-5) 

If the child was evicted, `find_by_previous` returns nothing and the child is silently dropped. The node never re-requests it. The victim's transaction chain is broken. [7](#0-6) 

---

### Impact Explanation

A legitimate user who sends a child transaction before its parent (a normal pattern in CKB's relay protocol, e.g., pre-signed transaction chains, DAO withdrawal chains, or HTLC chains) will have their child transaction silently evicted. When the parent is later confirmed or relayed, the child is not re-queued. The user's transaction chain stalls until they detect the failure and manually resubmit — which may itself be evicted again if the attacker maintains pool pressure. This is a targeted, repeatable denial of service against transaction propagation for specific users or transaction chains.

---

### Likelihood Explanation

The attack requires only a single connected P2P peer and the ability to send 100 relay messages containing syntactically valid transactions referencing non-existent parent hashes. No fees are required (orphan transactions are not verified for fee rate before being stored). The pool cap is only 100 entries — trivially saturated. With 100 attacker entries and 1 victim entry, the probability of evicting the victim on each insertion cycle is 100/101 ≈ 99%. The attacker can continuously rotate new orphan transactions to maintain saturation. The cost is negligible L2-equivalent bandwidth on CKB's cheap network.

---

### Recommendation

1. **Per-peer orphan quota**: Track how many orphan entries each `PeerIndex` has contributed. When the pool is full, evict from the peer with the most entries first (similar to Bitcoin Core's approach).
2. **Evict by declared cycle or fee rate**: Prefer evicting orphans with the lowest declared cycles or fee rate rather than random selection.
3. **Increase the cap or make it configurable**: `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` is very small for a busy node. [8](#0-7) 

---

### Proof of Concept

1. Attacker connects to a CKB node as a P2P relay peer.
2. Attacker sends 100 `RelayTransaction` messages, each containing a transaction whose inputs reference random non-existent `OutPoint`s (e.g., random 32-byte tx hashes). Each transaction passes non-contextual validation (valid structure, valid declared cycles).
3. Each transaction fails resolution with `OutPointError::Unknown` → `is_missing_input` returns `true` → `add_orphan` is called → orphan pool fills to 100.
4. Victim sends their legitimate child transaction (e.g., a DAO withdrawal phase-2 tx whose phase-1 tx is in-flight). It is added to the orphan pool, pushing count to 101.
5. `limit_size` fires: `HashMap::keys().next()` returns an arbitrary entry. With 100 attacker entries and 1 victim entry, the victim is evicted with ~99% probability.
6. Attacker immediately sends one more junk orphan to refill the slot, maintaining 100 attacker entries.
7. Victim's parent transaction arrives and is accepted. `process_orphan_tx` calls `find_by_previous` → returns empty (victim's child was evicted) → child is permanently lost from this node.
8. Victim's transaction chain is broken; the child must be resubmitted, where it will be evicted again. [2](#0-1) [4](#0-3)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L18-28)
```rust
#[derive(Debug, Clone)]
pub struct Entry {
    /// Transaction
    pub tx: TransactionView,
    /// peer id
    pub peer: PeerIndex,
    /// Declared cycles
    pub cycle: Cycle,
    /// Expire timestamp
    pub expires_at: u64,
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

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```
