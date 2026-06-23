### Title
Orphan Transaction Pool Flooding Allows Attacker to Evict Legitimate Orphan Transactions - (File: tx-pool/src/component/orphan.rs)

### Summary
The `OrphanPool` in CKB's tx-pool has a global hard cap of 100 entries with no per-peer rate limiting. An unprivileged relay peer can flood the pool with fake orphan transactions (referencing non-existent parent outputs), causing legitimate in-flight child transactions to be randomly evicted. Evicted transactions are silently dropped from the pool, breaking transaction chains for legitimate users without any notification to the original sender.

### Finding Description

The `OrphanPool` stores transactions whose parent inputs are not yet present in the chain or mempool. It enforces a global cap via `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. When the pool exceeds this limit, `limit_size()` evicts entries using `self.entries.keys().next()` — effectively the first key in HashMap iteration order, which is non-deterministic but provides no fairness guarantee and no per-peer accounting:

```rust
// tx-pool/src/component/orphan.rs
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

There is no per-peer limit anywhere in `add_orphan_tx`:

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
    // No per-peer accounting before insertion
    self.entries.insert(tx.proposal_short_id(), Entry::new(tx.clone(), peer, declared_cycle));
    // ...
    self.limit_size()
}
```

The attacker entry path uses the standard relay protocol:
1. Attacker sends `RelayTransactionHashes` with 100+ fake tx hashes to the victim node.
2. The node adds them to `unknown_tx_hashes` and issues `GetRelayTransactions` (via `ask_for_txs`).
3. Attacker responds with `RelayTransactions` containing 100 transactions each referencing a non-existent `OutPoint`.
4. Each transaction fails `_process_tx` with `is_missing_input`, triggering `add_orphan` in `after_process`:

```rust
// tx-pool/src/process.rs
if is_missing_input(reject) {
    self.send_result_to_relayer(TxVerificationResult::UnknownParents { peer, parents: tx.unique_parents() });
    self.add_orphan(tx, peer, declared_cycle).await;
}
```

5. The orphan pool is now saturated at 100 entries with attacker-controlled fake orphans.
6. A legitimate user's child transaction arrives (whose real parent is in-flight). It is added to the pool, triggering eviction of a random entry — which may be the legitimate child itself.
7. The evicted transaction's hash is sent as `TxVerificationResult::Reject` to the relayer, which calls `remove_from_known_txs`. The node removes it from its tx filter but does **not** re-request it, and the original sender has no signal to re-relay.
8. When the legitimate parent finally arrives and `process_orphan_tx` runs, the child is no longer in the orphan pool and is silently lost.

The `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` limit in `add_ask_for_txs` is far above the 100-entry orphan pool cap, so it provides no protection against this attack.

### Impact Explanation

Legitimate transaction chains are broken without any error surfaced to the user. A child transaction that was correctly relayed to the node is silently evicted from the orphan pool. When the parent arrives, `process_orphan_tx` finds no children to resolve. The child transaction must be re-relayed by the original peer, but the peer has no mechanism to detect the eviction. This causes transaction propagation failures and can delay or permanently prevent transaction confirmation for affected users. The attacker can sustain the attack continuously by re-sending fake orphans as the old ones expire (after `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL`).

**Impact: Medium** — transaction propagation is disrupted for legitimate users; no direct fund theft, but transaction chains are broken silently.

### Likelihood Explanation

The attack requires only a standard peer connection and the ability to send `RelayTransactionHashes` followed by `RelayTransactions` with structurally valid but semantically invalid transactions (non-existent inputs). No proof-of-work, no privileged keys, and no Sybil attack is required. The cost is 100 small transactions per attack cycle. The `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` cap is small enough that a single peer can saturate it in one request-response round trip.

**Likelihood: Medium** — low cost, standard peer access, no special privileges required.

### Recommendation

1. **Add per-peer orphan pool accounting**: Track how many orphan entries each peer has contributed and reject new entries from peers that have already filled their quota (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / expected_peers`).
2. **Replace random eviction with peer-weighted eviction**: When the pool is full, prefer to evict entries from the peer that has contributed the most orphans, not a random entry.
3. **Increase the orphan pool cap or make it configurable**: The current cap of 100 is very small for a network with many concurrent in-flight transaction chains.

### Proof of Concept

```
1. Attacker connects to victim CKB node as a relay peer.

2. Attacker sends RelayTransactionHashes with 100 fake tx hashes:
   [hash_1, hash_2, ..., hash_100]

3. Victim node adds them to unknown_tx_hashes and sends GetRelayTransactions
   back to the attacker.

4. Attacker responds with RelayTransactions containing 100 transactions,
   each spending a non-existent OutPoint (e.g., random_bytes(32), index=0).

5. Each transaction fails _process_tx with is_missing_input → added to
   OrphanPool. Pool is now at capacity (100 entries).

6. Legitimate user's child_tx (parent is in-flight) arrives via relay.
   add_orphan_tx is called → limit_size() evicts a random entry.
   child_tx may be the evicted entry.

7. Evicted child_tx hash is sent as TxVerificationResult::Reject →
   remove_from_known_txs. Node silently drops child_tx.

8. Legitimate parent_tx arrives → process_orphan_tx finds no children
   for parent_tx's outputs. child_tx is permanently lost from this node.

9. Attacker repeats step 2-4 every ~ORPHAN_TX_EXPIRE_TIME seconds to
   maintain pool saturation.
```

**Root cause**: `tx-pool/src/component/orphan.rs`, `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer limit and random eviction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** sync/src/relayer/mod.rs (L676-686)
```rust
                    TxVerificationResult::UnknownParents { peer, parents } => {
                        let tx_hashes: Vec<_> = {
                            let mut tx_filter = self.shared.state().tx_filter();
                            tx_filter.remove_expired();
                            parents
                                .into_iter()
                                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                                .collect()
                        };
                        self.shared.state().add_ask_for_txs(peer, tx_hashes);
                    }
```

**File:** sync/src/types/mod.rs (L1483-1529)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();

        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
        {
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
                }
                keyed_priority_queue::Entry::Vacant(entry) => {
                    entry.set_priority(UnknownTxHashPriority {
                        request_time: Instant::now(),
                        peers: vec![peer_index],
                        requested: false,
                    })
                }
            }
        }

        // Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );

            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
        }
```
