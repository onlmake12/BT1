### Title
Orphan Transaction Pool Exhaustion via Unbounded Per-Peer Insertion — (`File: tx-pool/src/component/orphan.rs`)

---

### Summary

The CKB orphan transaction pool (`OrphanPool`) has a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries. When the pool is full, eviction selects a victim using `self.entries.keys().next()` — effectively the first key in HashMap iteration order, with no per-peer fairness or rate-limiting. Any connected P2P peer can relay structurally valid transactions referencing non-existent inputs (zero-cost orphans), fill all 100 slots, and cause legitimate orphan transactions to be continuously evicted. This is a direct analog to the delegation-service DoS: a fixed-size admission list is exhausted by a single attacker, blocking legitimate participants.

---

### Finding Description

In `tx-pool/src/component/orphan.rs`, the constant `DEFAULT_MAX_ORPHAN_TRANSACTIONS` is set to 100: [1](#0-0) 

When `add_orphan_tx` is called, the transaction is inserted unconditionally (if not a duplicate), and then `limit_size()` is called to enforce the cap: [2](#0-1) 

Inside `limit_size()`, when the pool exceeds 100 entries, eviction picks the first key from the HashMap — there is no per-peer accounting, no fee-rate ordering, and no protection for recently-inserted legitimate entries: [3](#0-2) 

The attacker entry path is through the P2P relay protocol. In `tx-pool/src/process.rs`, when a relayed transaction fails resolution with a missing-input error, it is unconditionally added to the orphan pool: [4](#0-3) 

The `add_orphan` call in `TxPoolService` holds no per-peer quota: [5](#0-4) 

Non-contextual verification (the only gate before orphan insertion) checks transaction structure but cannot check fee rate because input values are unknown for orphan transactions: [6](#0-5) 

The `OrphanPool` stores entries keyed by `ProposalShortId` with no per-peer slot tracking: [7](#0-6) 

---

### Impact Explanation

An attacker with a single P2P connection can relay 100 structurally valid transactions whose inputs reference non-existent `OutPoint`s. These cost zero CKB (no inputs to spend, no fees calculable). Once the pool is saturated, every legitimate orphan transaction submitted by honest peers has a high probability of being evicted immediately (or the attacker's entries are evicted and the attacker re-fills). Legitimate orphan transactions — which arise naturally when a child transaction is relayed before its parent — are silently dropped. The relay layer marks them as `Reject` and clears them from the bloom filter: [8](#0-7) 

This means the honest peer's transaction is not re-requested, causing silent relay failure. The attack is cheap to sustain: the attacker simply re-submits fake orphans whenever any slot opens.

---

### Likelihood Explanation

The attack requires only a single connected P2P peer slot (inbound or outbound). CKB nodes accept up to `max_peers = 125` connections by default: [9](#0-8) 

Any unprivileged network peer can perform this attack. The cost is zero CKB tokens and minimal bandwidth (100 small transactions). The orphan pool expiry time is `100 * MAX_BLOCK_INTERVAL`, meaning fake entries persist for a long time without the attacker needing to reconnect: [10](#0-9) 

---

### Recommendation

1. **Per-peer orphan quota**: Track how many orphan slots each `PeerIndex` occupies. Evict from the peer with the most entries first, rather than using HashMap iteration order.
2. **Evict the sender's own entry on overflow**: When the pool is full and a new orphan arrives from peer P, if P already holds `k` entries and the eviction candidate also belongs to P, prefer evicting from P's existing entries.
3. **Minimum declared-cycle threshold for orphan admission**: Require a non-zero declared cycle count to raise the cost of fake orphan spam.
4. **Rate-limit orphan submissions per peer**: Track orphan submission rate per `PeerIndex` and ban peers that exceed a threshold within a time window.

---

### Proof of Concept

1. Establish a P2P connection to a target CKB node (relay protocol v3).
2. Craft 100 transactions, each with one input referencing a random non-existent `OutPoint` (e.g., `tx_hash = random_bytes(32)`, `index = 0`), one output sending 61 CKB to any lock script, and a declared cycle count of 1.
3. Relay all 100 transactions via `RelayTransactions` messages. Each passes non-contextual verification (valid structure) and fails contextual resolution with `OutPointError::Unknown`, triggering `add_orphan`.
4. The orphan pool is now full with the attacker's 100 entries.
5. An honest peer relays a legitimate orphan transaction (e.g., a child whose parent is in-flight). It is inserted (pool size = 101), `limit_size()` fires, and one entry is evicted via `self.entries.keys().next()`. The legitimate transaction may be the evicted one.
6. The attacker monitors evictions (via the `TxVerificationResult::Reject` signal sent back) and immediately re-submits the evicted fake orphan to maintain 100 slots.
7. Legitimate orphan transactions are continuously dropped; the relay layer marks them unknown and does not re-request them.

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
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

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```

**File:** tx-pool/src/component/orphan.rs (L134-158)
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
```

**File:** tx-pool/src/process.rs (L401-412)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
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

**File:** resource/ckb.toml (L94-94)
```text
max_peers = 125
```
