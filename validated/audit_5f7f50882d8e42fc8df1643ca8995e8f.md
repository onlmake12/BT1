[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

Audit Report

## Title
Orphan Tx-Pool Griefing via No Per-Peer Limit Allows Attacker to Evict Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` enforces a global cap of 100 entries with no per-peer submission accounting. An unprivileged relay peer can fill all 100 slots with fabricated orphan transactions referencing non-existent parent outputs, causing random eviction of legitimate orphan transactions. Evicted orphans are sent as `TxVerificationResult::Reject` to the relay layer, marking them as rejected in the relay filter and preventing automatic re-fetching when their parent is confirmed.

## Finding Description
The `OrphanPool` struct stores entries in a flat `HashMap<ProposalShortId, Entry>` with no per-peer tracking:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
``` [1](#0-0) 

The global cap is `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`, and `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` (~50 minutes): [2](#0-1) 

When the pool is full, `limit_size()` evicts entries using `HashMap::keys().next()`, which is effectively random (HashMap iteration order is non-deterministic):

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
``` [3](#0-2) 

`add_orphan_tx` performs no per-peer quota check before inserting: [4](#0-3) 

In `process.rs`, `add_orphan` sends `TxVerificationResult::Reject` for every evicted transaction hash, causing the relay layer to mark it as rejected/unknown in its filter: [5](#0-4) 

The `after_process` path confirms that any transaction failing with `is_missing_input` is routed to `add_orphan`, making the attack path reachable from a standard relay peer submission: [6](#0-5) 

When the legitimate parent is later confirmed, `process_orphan_tx` performs a BFS via `find_orphan_by_previous`, but the evicted child is no longer present, so it is never promoted to pending: [7](#0-6) 

## Impact Explanation
This matches the allowed CKB bounty impact: **High — bad design which could cause CKB network congestion with few costs**. An attacker connecting to multiple nodes simultaneously can systematically prevent orphan transaction propagation across the network. Legitimate child transactions are silently dropped from the orphan pool and marked as rejected in the relay filter, meaning they will not be re-fetched when their parent confirms. Users must manually detect and re-submit their transactions. During periods of network congestion when orphan transactions are common (e.g., high-throughput bursts), this disrupts normal transaction flow for all honest users connected to attacked nodes.

## Likelihood Explanation
Any unprivileged peer with a standard P2P relay connection can execute this attack. No keys, funds, or special access are required — orphan admission does not verify input existence, so 100 transactions with fabricated (random) input out-points are sufficient to fill the pool. The attack is trivially automatable and can be sustained indefinitely by refreshing fake orphans before `ORPHAN_TX_EXPIRE_TIME` (~50 minutes) expires. The attacker can connect to many nodes simultaneously, amplifying the network-wide impact.

## Recommendation
1. **Add per-peer orphan accounting**: Track the count of orphan entries per `PeerIndex`. Reject new orphan submissions from peers that have already contributed more than `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_connected_peers` entries.
2. **Replace random eviction with peer-weighted eviction**: In `limit_size()`, instead of `self.entries.keys().next()`, identify the peer with the most entries and evict one of their orphans first, making flooding self-defeating.
3. **Apply a minimum fee-rate check at orphan admission**: Orphans with zero or sub-`min_fee_rate` fees are cheap to fabricate; rejecting them at `add_orphan_tx` raises the cost of the attack.

## Proof of Concept
```
1. Attacker connects to a CKB node as a relay peer (no privilege required).
2. Attacker constructs 100 transactions T_1..T_100, each spending a distinct
   fabricated out-point (random tx_hash, index=0). Each has valid structure
   but references a non-existent parent — no on-chain funds needed.
3. Attacker sends RelayTransactionHashes for T_1..T_100.
   Node requests them; attacker sends via RelayTransactions.
   Each fails with is_missing_input=true → add_orphan() → add_orphan_tx().
   After 100 submissions, OrphanPool.len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS.
4. Honest user U submits child transaction C (parent P not yet in pool).
   C also fails with is_missing_input → add_orphan_tx() → limit_size() triggers
   random eviction. C may be evicted immediately.
   Evicted tx hash → send_result_to_relayer(TxVerificationResult::Reject{C.hash})
   → C marked as rejected/unknown in relay filter.
5. Parent P is confirmed in a block. process_orphan_tx(P) is called.
   find_by_previous(P) returns empty (C was evicted). C is never promoted.
6. U's transaction C is silently lost. U must manually detect and re-submit.
7. Attacker refreshes T_1..T_100 before ORPHAN_TX_EXPIRE_TIME to sustain
   the attack indefinitely at negligible cost.
```

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
