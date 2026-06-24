All code references check out exactly. Let me confirm the relay filter behavior for evicted transactions.Audit Report

## Title
Orphan Pool Flooding via Unbounded Per-Peer Submission Evicts Legitimate Orphan Transactions - (File: `tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries. When full, `limit_size()` evicts entries by arbitrary HashMap iteration order with no per-peer quota. A single unprivileged P2P peer can saturate all 100 slots with junk orphan transactions (referencing non-existent inputs), probabilistically or deterministically evicting legitimate orphan transactions. When the legitimate parent later arrives, `process_orphan_tx` cannot find the evicted child via `find_by_previous` and silently drops it.

## Finding Description

**Root cause:** `limit_size()` in `tx-pool/src/component/orphan.rs` evicts by arbitrary HashMap iteration order and never consults the `peer` field stored in each `Entry`:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
``` [1](#0-0) 

The `Entry` struct stores `peer: PeerIndex` but it is never used during eviction or admission decisions: [2](#0-1) 

**Entry path:** In `tx-pool/src/process.rs`, any relayed transaction whose inputs are missing is unconditionally added to the orphan pool: [3](#0-2) 

`add_orphan` calls `add_orphan_tx` which inserts the entry and then calls `limit_size()`, returning hashes of evicted transactions. Evicted hashes are sent as `TxVerificationResult::Reject` to the relayer: [4](#0-3) 

In `sync/src/relayer/mod.rs`, `TxVerificationResult::Reject` causes `remove_from_known_txs`, removing the evicted tx from the node's known-tx set: [5](#0-4) 

**Why existing checks fail:** The relay rate limiter is keyed by `(PeerIndex, message_item_id)` at 30 messages/second per peer per message type: [6](#0-5) 

Since each `RelayTransactions` message can carry multiple transactions, 100 orphan slots are saturated in a handful of messages, well within the rate limit. There is no per-peer cap on orphan pool occupancy anywhere in `add_orphan_tx` or `limit_size()`. [7](#0-6) 

**Silent drop:** `process_orphan_tx` resolves children via `find_by_previous`, which queries `by_out_point`. When `remove_orphan_tx` evicts a child, it removes the child's entries from both `entries` and `by_out_point`. When the parent later arrives, `find_by_previous` returns empty and the child is permanently lost from this node's perspective: [8](#0-7) [9](#0-8) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker operating a single P2P connection can continuously flood the orphan pool across any reachable CKB node, preventing all orphan transactions from surviving long enough for their parents to arrive. Applied simultaneously to multiple nodes, this degrades the network's ability to process child-before-parent transaction patterns (a common CKB usage pattern in two-phase commit flows), causing persistent transaction drops and forcing users to repeatedly resubmit. The attack is cheap to sustain indefinitely.

## Likelihood Explanation

The attack requires only a standard P2P connection and the ability to craft transactions with arbitrary `OutPoint` inputs referencing non-existent cells — trivially possible with no keys, funds, or privileged access. The orphan pool cap of 100 is small. The relay rate limit of 30 `RelayTransactions` messages/second per peer, each carrying multiple transactions, allows the pool to be saturated in under a second. The attack is fully repeatable and stateless.

## Recommendation

1. **Per-peer admission cap:** In `add_orphan_tx`, count existing entries from the submitting peer. If a single peer already holds `pool_size / max_peers` entries (e.g., `100 / 8 = 12`), reject the new entry rather than evicting a random existing one.
2. **Peer-aware eviction:** In `limit_size()`, when forced to evict, prefer entries from the peer with the highest current occupancy in the pool (use the stored `entry.peer` field). This mirrors Bitcoin Core's orphan pool per-peer eviction strategy.
3. **Optionally:** Disconnect or apply a score penalty to peers that repeatedly trigger `limit_size()`.

## Proof of Concept

1. Connect to a CKB node as a P2P peer via `RelayV3`.
2. Honest user relays `child_tx` (spending an output of `parent_tx` not yet in the pool). `child_tx` enters the orphan pool (size: 1).
3. Attacker crafts 200 transactions `atk_tx[0..199]`, each with inputs referencing random non-existent `OutPoint`s (`OutPoint::new(random_hash, 0)`).
4. Attacker sends them via `RelayTransactions` messages (batched, within rate limit). Each is rejected with `is_missing_input` and added to the orphan pool.
5. After the 100th attacker transaction, `limit_size()` fires repeatedly. With 100 attacker entries and 1 legitimate entry, each eviction round has ~1% chance of hitting `child_tx`. After 200+ attacker transactions, the probability of `child_tx` surviving approaches zero.
6. Attacker's junk transactions occupy all 100 slots.
7. `parent_tx` is confirmed and relayed. `process_orphan_tx` calls `find_by_previous(&parent_tx)` — returns empty because `child_tx` was removed from `by_out_point` during eviction.
8. `child_tx` is permanently dropped. User must resubmit manually.
9. Attacker repeats continuously to prevent any resubmission from surviving.

A unit test can be written directly against `OrphanPool::add_orphan_tx` by inserting one legitimate entry, then calling `add_orphan_tx` 200 times with distinct junk transactions from a single peer index, and asserting that `find_by_previous` on the legitimate parent returns empty.

### Citations

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

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```
