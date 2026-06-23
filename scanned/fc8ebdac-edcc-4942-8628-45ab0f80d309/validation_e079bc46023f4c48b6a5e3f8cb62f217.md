### Title
Attacker Can Flood OrphanPool to Evict Legitimate Orphan Transactions via Unbounded Per-Peer Submission - (File: `tx-pool/src/component/orphan.rs`)

### Summary
The `OrphanPool` in CKB's tx-pool is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries. When the pool is full, `limit_size()` evicts entries **randomly** (via HashMap iteration order) with no per-peer quota. An unprivileged P2P peer can craft and relay 100+ transactions referencing non-existent parent inputs, filling the orphan pool and probabilistically evicting legitimate orphan transactions submitted by honest users. When the legitimate parent transactions later arrive, `process_orphan_tx` cannot find the evicted children and they are silently dropped.

### Finding Description

In `tx-pool/src/component/orphan.rs`, the `OrphanPool` enforces a hard cap:

```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

When `add_orphan_tx` is called and the pool exceeds this limit, `limit_size()` is invoked:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

The eviction is random (HashMap iteration order). There is **no per-peer quota** — the `peer: PeerIndex` field stored in each `Entry` is never consulted during eviction or admission. A single peer can submit all 100 slots.

The entry path is the P2P relay protocol. In `tx-pool/src/process.rs`, when a relayed transaction has a missing input it is unconditionally added to the orphan pool:

```rust
if is_missing_input(reject) {
    self.send_result_to_relayer(TxVerificationResult::UnknownParents { ... });
    self.add_orphan(tx, peer, declared_cycle).await;
}
```

`add_orphan` calls `add_orphan_tx` which calls `limit_size()`, returning the hashes of evicted transactions. Back in `process.rs`:

```rust
for tx_hash in evicted_txs {
    self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
}
```

Evicted transactions are marked `Reject` in the relay filter, meaning the node will not re-request them from peers. The relay rate limiter in `sync/src/relayer/mod.rs` allows 30 messages/second per peer per message type, but each `RelayTransactions` message can carry multiple transactions, so 100 orphan slots can be saturated in a few seconds.

### Impact Explanation

A legitimate user submits a child transaction before its parent is confirmed (a common pattern in CKB's two-phase commit). The child lands in the orphan pool. An attacker then floods the orphan pool with 100+ junk transactions (referencing non-existent inputs). The legitimate child is evicted. When the parent transaction is later relayed and accepted, `process_orphan_tx` calls `find_by_previous` which looks up `by_out_point` — but the evicted child's entry was removed from both `entries` and `by_out_point`, so it is never found. The child transaction is permanently dropped from the node's perspective and must be resubmitted by the user. The attacker can sustain this continuously, preventing any orphan transaction from surviving long enough for its parent to arrive.

### Likelihood Explanation

The attack requires only a single P2P connection and the ability to craft transactions with arbitrary (non-existent) input `OutPoint`s, which is trivially possible. The orphan pool size is only 100, and the relay rate limit (30 msg/s) is high enough to fill it in seconds. No funds, keys, or privileged access are required. The attack is cheap to sustain indefinitely.

### Recommendation

Enforce a per-peer cap on orphan pool entries. When `limit_size()` must evict, prefer evicting entries from the peer that has the most entries in the pool (i.e., the peer most likely flooding). Additionally, when the pool is full and a new orphan arrives from a peer that already holds `N` entries where `N >= pool_size / max_peers`, reject the new entry rather than evicting a random existing one. This mirrors the fix pattern used in Bitcoin Core's orphan pool (per-peer limits).

### Proof of Concept

1. Attacker connects to a CKB node as a P2P peer via `RelayV3` protocol.
2. Honest user relays a child transaction `child_tx` (spending output of `parent_tx` which is not yet in the pool). `child_tx` enters the orphan pool (pool size: 1).
3. Attacker crafts 100 transactions `atk_tx[0..99]`, each with inputs referencing random non-existent `OutPoint`s (e.g., `OutPoint::new(random_hash, 0)`).
4. Attacker sends all 100 via `RelayTransactions` messages. Each is rejected with `is_missing_input` and added to the orphan pool.
5. After the 100th attacker transaction, `limit_size()` fires. The pool has 101 entries; one random entry is evicted. With 100 attacker entries and 1 legitimate entry, the legitimate `child_tx` is evicted with ~1% probability per eviction round. Repeating with 200+ attacker transactions drives the probability of eviction to near certainty.
6. Attacker's junk transactions now occupy all 100 slots.
7. Honest user's `parent_tx` is confirmed and relayed. `process_orphan_tx` calls `find_by_previous(&parent_tx)` — returns empty because `child_tx` was evicted and removed from `by_out_point`.
8. `child_tx` is never processed. The user must resubmit it manually.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/process.rs (L563-572)
```rust
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
