### Title
Orphan Transaction Pool Exhaustion via Unbounded Per-Peer Submissions — (File: `tx-pool/src/component/orphan.rs`)

---

### Summary

The `OrphanPool` enforces a global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries with **no per-peer accounting or rate limiting**. Any unprivileged peer (or RPC caller) can fill the entire orphan pool with crafted junk transactions, causing legitimate orphan transactions to be randomly evicted. When a legitimate orphan's parent later arrives, the orphan is gone and will not be automatically promoted, breaking the normal orphan-resolution flow.

---

### Finding Description

`tx-pool/src/component/orphan.rs` defines the `OrphanPool` struct and its admission logic:

```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
``` [1](#0-0) 

The `add_orphan_tx()` function accepts any transaction from any peer without tracking per-peer submission counts:

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
    ...
    // DoS prevention: do not allow OrphanPool to grow unbounded
    self.limit_size()
}
``` [2](#0-1) 

When the pool is full, `limit_size()` evicts entries using `HashMap::keys().next()` — effectively arbitrary ordering — with no preference for evicting the submitting peer's entries:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
``` [3](#0-2) 

The `OrphanPool` struct itself has no per-peer counter field:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
``` [4](#0-3) 

The `add_orphan` call path in `tx-pool/src/process.rs` passes the peer index for record-keeping only — it is never used to enforce a per-peer quota:

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
``` [5](#0-4) 

---

### Impact Explanation

A malicious peer submits 100 transactions whose inputs reference non-existent UTXOs. These are classified as orphan transactions and fill the pool to its hard cap. Every subsequent legitimate orphan transaction submitted by honest peers triggers an eviction of an arbitrary existing entry. If the attacker continuously replenishes the pool (trivially cheap — no fees are required for transactions that never reach the mempool), legitimate orphan transactions are perpetually evicted. When the parent of a legitimate orphan is later confirmed in a block, `process_orphan_tx()` looks up the orphan by its short ID and finds nothing, so the child transaction is silently dropped and must be resubmitted by the original sender. Under sustained attack, honest users' orphan transactions can never complete the orphan-resolution path. [6](#0-5) 

---

### Likelihood Explanation

The attack requires only a P2P connection (or repeated `send_transaction` RPC calls with transactions referencing non-existent inputs). No fees, no keys, and no privileged access are needed. Submitting 100 transactions is trivial. The attacker must sustain the attack (re-fill after evictions), but each refill costs nothing beyond bandwidth. The global cap of 100 is small enough that a single peer can monopolize it entirely. [1](#0-0) 

---

### Recommendation

Add per-peer accounting to `OrphanPool`. Track how many orphan entries each `PeerIndex` has contributed. When the pool is full, prefer to evict entries from the peer with the highest count rather than evicting arbitrarily. Cap each peer's contribution to `DEFAULT_MAX_ORPHAN_TRANSACTIONS / expected_max_peers`. This mirrors the fix applied in the referenced ERC20Guild report: restrict the state-modifying path to authorized callers (or, in CKB's case, to a fair per-peer share). [7](#0-6) 

---

### Proof of Concept

1. Connect to a CKB node as an unprivileged P2P peer or via the `send_transaction` RPC.
2. Construct 100 transactions whose `inputs` reference `OutPoint`s that do not exist on-chain or in the mempool (e.g., random 32-byte hashes). Set `declared_cycle` to any valid value.
3. Submit all 100 transactions. Each is routed to `add_orphan_tx()` and fills the pool to `DEFAULT_MAX_ORPHAN_TRANSACTIONS`.
4. A legitimate user submits an orphan transaction (e.g., a child of a transaction currently being relayed). `add_orphan_tx()` inserts it and immediately calls `limit_size()`, which evicts an arbitrary entry — potentially the legitimate one.
5. The attacker monitors evictions (via the returned `evicted_txs` hashes propagated back through `send_result_to_relayer`) and resubmits a replacement junk transaction to keep the pool saturated.
6. When the legitimate orphan's parent is confirmed, `process_orphan_tx()` finds no matching entry in the pool and silently skips promotion. The child transaction is lost until the original sender resubmits it. [2](#0-1) [5](#0-4)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L42-45)
```rust
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
