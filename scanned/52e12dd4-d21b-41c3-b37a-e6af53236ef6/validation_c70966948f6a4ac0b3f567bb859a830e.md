Audit Report

## Title
Orphan Tx-Pool Griefing via No Per-Peer Limit Allows Attacker to Evict Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary

The `OrphanPool` enforces a global cap of 100 entries (`DEFAULT_MAX_ORPHAN_TRANSACTIONS`) with no per-peer submission accounting. An unprivileged relay peer can flood all 100 slots with fake orphan transactions referencing fabricated non-existent inputs. When the pool is full, legitimate orphans are randomly evicted and immediately marked as `Reject` in the relay filter, causing them to be silently dropped and never re-promoted when their parent is confirmed.

## Finding Description

**Root cause — `tx-pool/src/component/orphan.rs`:**

The `OrphanPool` struct stores entries in a flat `HashMap` with no per-peer tracking:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
``` [1](#0-0) 

The global cap is `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. [2](#0-1) 

`add_orphan_tx()` performs no per-peer check before inserting: [3](#0-2) 

When the pool exceeds 100, `limit_size()` evicts entries **randomly** using HashMap iteration order — no fee-rate priority, no per-peer fairness:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
``` [4](#0-3) 

**Eviction consequence — `tx-pool/src/process.rs`:**

Every evicted orphan hash is sent as `TxVerificationResult::Reject` to the relay layer, marking it as unknown/rejected in the bloom filter. The node will not re-request it from peers: [5](#0-4) 

**Orphan promotion failure — `tx-pool/src/process.rs`:**

`process_orphan_tx()` performs a BFS via `find_orphan_by_previous()`. If the legitimate child was evicted, it is absent from `by_out_point` and is never promoted to pending when its parent is confirmed: [6](#0-5) 

**Orphan admission — `tx-pool/src/util.rs`:**

`is_missing_input()` admits any transaction whose inputs resolve to an unknown out-point — fabricated inputs trivially satisfy this: [7](#0-6) 

**Attack path:**
1. Attacker connects as an unprivileged relay peer.
2. Attacker sends 100 transactions `T_1..T_100`, each spending a distinct fabricated out-point. Each passes `is_missing_input` and is admitted via `add_orphan_tx`. Pool reaches capacity.
3. Any subsequent legitimate orphan `C` triggers `limit_size()`. Either `C` itself or one of the attacker's entries is randomly evicted. If `C` is evicted, `TxVerificationResult::Reject{C.hash}` is sent — `C` is marked unknown in the relay filter.
4. When `C`'s parent `P` is confirmed, `process_orphan_tx(P)` finds nothing in `by_out_point` for `P`'s outputs. `C` is never promoted.
5. Attacker's fake orphans persist for `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` (~50 minutes) and can be refreshed before expiry to sustain the attack indefinitely.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker with a single P2P connection and 100 zero-fee (no on-chain funds required) transactions can continuously disrupt orphan transaction processing on any targeted node. Legitimate users' child transactions are silently dropped and must be manually re-submitted. Deployed against multiple nodes simultaneously, this degrades orphan resolution network-wide during periods of high orphan activity (e.g., network congestion or fast block propagation).

## Likelihood Explanation

Any unprivileged peer reachable via the relay protocol can execute this attack. No keys, on-chain funds, or special access are required — orphan admission does not verify input existence. The attack is trivially automatable (100 minimal transactions with random fabricated inputs) and can be sustained indefinitely by refreshing before `ORPHAN_TX_EXPIRE_TIME` expires.

## Recommendation

1. **Add per-peer orphan quota**: Track `peer → count` in `OrphanPool`. Reject `add_orphan_tx` if the submitting peer already holds `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_connected_peers` entries.
2. **Evict from the most-contributing peer**: Replace the random eviction in `limit_size()` with a policy that preferentially evicts from the peer with the most entries, making flooding self-defeating.
3. **Apply minimum fee-rate check before orphan admission**: Reject orphans below `min_fee_rate` before inserting into the pool, raising the cost of fabricating fake orphans.

## Proof of Concept

```
1. Attacker connects to a CKB node as a relay peer.
2. Attacker constructs 100 transactions T_1..T_100, each spending a distinct
   fabricated out-point (tx_hash=random_bytes, index=0). Valid structure,
   non-existent parent.
3. Attacker sends RelayTransactionHashes for T_1..T_100.
   Node requests them; attacker sends via RelayTransactions.
   Each fails resolution with is_missing_input=true → added to OrphanPool.
   After 100 submissions: OrphanPool.len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS.
4. Honest user U submits child transaction C (parent P not yet in pool).
   C is also missing input → add_orphan_tx called → limit_size() triggers.
   Random eviction: if C is evicted → send_result_to_relayer(Reject{C.hash}).
   C is now marked unknown in relay filter.
5. Parent P is confirmed in a block. process_orphan_tx(P) is called.
   find_orphan_by_previous(P) returns empty (C was evicted and removed from
   by_out_point). C is never promoted to pending.
6. U's transaction C is silently lost. U must manually re-submit.
7. Attacker refreshes T_1..T_100 before ORPHAN_TX_EXPIRE_TIME (~50 min)
   to sustain the attack indefinitely.
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

**File:** tx-pool/src/process.rs (L591-597)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
            for orphan in orphans.into_iter() {
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```
