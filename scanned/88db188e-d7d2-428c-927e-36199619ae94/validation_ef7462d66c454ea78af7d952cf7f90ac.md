Audit Report

## Title
Orphan Transaction Pool Slot Exhaustion via No Per-Peer Limit — (`tx-pool/src/component/orphan.rs`)

## Summary
`OrphanPool` enforces a global cap of 100 entries with no per-peer quota. A single unprivileged P2P peer can fill all 100 slots with structurally valid transactions referencing non-existent `OutPoint`s. When the pool is full, `limit_size()` evicts one entry at random, and the attacker immediately re-fills the evicted slot, maintaining near-total occupancy and preventing legitimate orphan transactions from persisting long enough to be promoted.

## Finding Description
`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` is the sole global cap ( [1](#0-0) ). When the pool is full, `limit_size()` evicts the first key returned by HashMap iteration — effectively random — with no preference for entries from the most-contributing peer ( [2](#0-1) ). `add_orphan_tx` accepts entries from any peer with no per-peer accounting or quota check ( [3](#0-2) ).

The reachable exploit path is: remote peer sends `RelayTransactions` → `non_contextual_verify` passes (structural checks only, no input existence check) → resolution fails with `Reject::Resolve(out_point_err)` where `out_point_err.is_unknown()` is true → `is_missing_input` returns `true` → `add_orphan` is called ( [4](#0-3) ; [5](#0-4) ). No CKB balance or signature is required before orphan insertion.

Orphan entries persist for up to `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` seconds since the fake parent OutPoints never appear on-chain ( [6](#0-5) ). There is no existing per-peer limit anywhere in the orphan pool code path.

## Impact Explanation
Matches **"High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attack requires zero CKB balance — only a live P2P connection and structurally valid transactions. When applied to multiple nodes simultaneously, legitimate child transactions that arrive before their parents cannot be stored in the orphan pool. When the parent later arrives, `process_orphan_tx` finds no children to promote, and the child transaction is silently dropped from the relay graph unless independently re-relayed. This degrades transaction relay reliability across targeted nodes at negligible attacker cost.

## Likelihood Explanation
Low-to-medium. The attacker needs an established P2P connection (standard for any network participant) and must continuously re-submit replacement orphan transactions as their slots are evicted. No funds, no signatures, no special privileges are required. The attack is cheap to sustain indefinitely and can be parallelized across multiple target nodes.

## Recommendation
Add per-peer accounting to `OrphanPool`. Track a `HashMap<PeerIndex, usize>` of entry counts per peer. In `limit_size()`, prefer evicting from the peer with the most entries (analogous to the largest-group eviction strategy in `PeerRegistry::try_evict_inbound_peer` at [7](#0-6) ). Alternatively, cap per-peer orphan entries at `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_inbound_peers` and reject new orphan submissions from a peer that has reached its quota in `add_orphan_tx`.

## Proof of Concept
1. Attacker establishes a standard P2P connection to the target CKB node using the Relay protocol.
2. Attacker crafts 100 structurally valid CKB transactions, each with one input referencing a random non-existent `OutPoint` (random `tx_hash`, index 0). No CKB balance or valid signatures are needed.
3. Each transaction is sent via `RelayTransactions`. Each passes `non_contextual_verify`, enters the verify queue, fails resolution with `OutPointError::Unknown`, satisfies `is_missing_input`, and is inserted into the orphan pool via `add_orphan_tx`.
4. After 100 submissions, `OrphanPool::len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS`.
5. Any legitimate orphan from an honest peer triggers `limit_size()`, which randomly evicts one attacker entry (100/101 probability of evicting an attacker entry). The attacker immediately sends a replacement, restoring full occupancy.
6. Legitimate orphan transactions are evicted within one round-trip and are never promoted when their parents arrive. This can be verified with a unit test that fills the orphan pool from a single `PeerIndex`, inserts a legitimate orphan from a different peer, and asserts it is immediately evicted.

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-15)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
```

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
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

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
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
