### Title
Orphan Transaction Pool Slot Exhaustion DoS via No Per-Peer Limit — (`tx-pool/src/component/orphan.rs`)

### Summary

The `OrphanPool` has a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries and no per-peer quota. A single unprivileged P2P peer can fill all 100 slots with attacker-controlled orphan transactions referencing non-existent parent OutPoints. When the pool is full, new legitimate orphan transactions from honest peers are evicted at random. The attacker can continuously re-fill evicted slots, maintaining near-total occupancy and blocking legitimate orphan transaction storage for the duration of the attack.

### Finding Description

`OrphanPool` in `tx-pool/src/component/orphan.rs` enforces a global cap of 100 entries: [1](#0-0) 

When the pool is full, `limit_size()` evicts **one random entry** (HashMap iteration order) to make room for the new one: [2](#0-1) 

`add_orphan_tx` accepts entries from any peer with no per-peer quota check: [3](#0-2) 

The entry path from a remote peer is: `TransactionsProcess::execute` → `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` → worker calls `_process_tx` → resolve fails with missing input → `after_process` calls `add_orphan`: [4](#0-3) 

The attacker crafts 100 structurally valid CKB transactions each referencing a non-existent `OutPoint`. These pass `non_contextual_verify` (structural checks only), enter the verify queue, fail resolution with `OutPointError::Unknown` (a missing-input error), and land in the orphan pool. Since the parents never exist on-chain, these entries persist for up to `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` seconds: [5](#0-4) 

When a legitimate orphan arrives and the pool is full, one of the 100 attacker entries is evicted (100/101 probability). The attacker immediately re-submits a replacement orphan, maintaining near-100% pool occupancy. Legitimate orphan transactions are evicted almost immediately after insertion.

### Impact Explanation

**High**: Legitimate transactions that arrive out-of-order (child before parent, common during relay) cannot be stored in the orphan pool. When the parent transaction later arrives, the child is absent from the pool and `process_orphan_tx` finds nothing to promote. The child transaction must be re-relayed independently, which may not happen automatically. This disrupts transaction relay across the node, causing legitimate transactions to be silently dropped from the relay graph for the duration of the attack.

### Likelihood Explanation

**Low**: The attacker needs an established P2P connection to the target node and must continuously send replacement orphan transactions as their slots are evicted. No CKB balance is required — only valid transaction structure (no signature verification occurs before orphan insertion). The attack is cheap to sustain but requires maintaining a live P2P session.

### Recommendation

Add a per-peer quota to `OrphanPool`. Track how many orphan entries each `PeerIndex` has contributed. When the pool is full, prefer evicting entries from the peer with the most entries (largest-group eviction), analogous to the inbound peer eviction strategy in `PeerRegistry::try_evict_inbound_peer`: [6](#0-5) 

Alternatively, cap the number of orphan entries per peer (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_inbound_peers`) and reject new orphan submissions from a peer that has reached its quota.

### Proof of Concept

1. Attacker establishes a P2P connection to the target CKB node using the Relay protocol.
2. Attacker crafts 100 structurally valid CKB transactions, each with one input referencing a random non-existent `OutPoint` (fake `tx_hash`, index 0). No CKB balance is needed.
3. Attacker sends each transaction via `RelayTransactions`. Each passes `non_contextual_verify`, enters the verify queue, fails with `OutPointError::Unknown`, and is added to the orphan pool via `add_orphan`.
4. After 100 submissions, `OrphanPool::len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS`.
5. Any legitimate orphan transaction from an honest peer triggers `limit_size()`, which randomly evicts one of the attacker's entries. The attacker immediately sends a replacement orphan, restoring full occupancy.
6. Legitimate orphan transactions are evicted within one round-trip, preventing them from being promoted when their parents arrive. Transaction relay for out-of-order transactions is disrupted for the duration of the attack.

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
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

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** network/src/peer_registry.rs (L191-210)
```rust
        let evict_group = candidate_peers
            .into_iter()
            .fold(
                HashMap::new(),
                |mut groups: HashMap<Group, Vec<&Peer>>, peer| {
                    groups.entry(peer.network_group()).or_default().push(peer);
                    groups
                },
            )
            .values()
            .max_by_key(|group| group.len())
            .cloned()
            .unwrap_or_default();

        // randomly evict a peer
        let mut rng = thread_rng();
        evict_group.choose(&mut rng).map(|peer| {
            debug!("Disconnect inbound peer {:?}", peer.connected_addr);
            peer.session_id
        })
```
