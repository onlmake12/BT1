Audit Report

## Title
Orphan Transaction Pool Exhaustion via Unbounded Per-Peer Insertion — (`File: tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` enforces a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries with no per-peer quota. Eviction selects the first key from a `HashMap` with no fairness guarantee. Any single P2P peer can fill all 100 slots with structurally valid but unresolvable transactions at zero CKB cost, causing legitimate orphan transactions from honest peers to be silently evicted and permanently dropped from relay.

## Finding Description
`DEFAULT_MAX_ORPHAN_TRANSACTIONS` is set to 100 with no per-peer accounting: [1](#0-0) 

`OrphanPool` stores entries keyed by `ProposalShortId` with no per-peer slot tracking: [2](#0-1) 

`add_orphan_tx` inserts unconditionally (if not a duplicate) then calls `limit_size()`: [3](#0-2) 

`limit_size()` evicts by taking the first key from the `HashMap` — no fee-rate ordering, no per-peer fairness, no protection for recently-inserted legitimate entries: [4](#0-3) 

The attacker entry path: in `after_process`, when contextual resolution fails with a missing-input error, `add_orphan` is called unconditionally with no per-peer quota check: [5](#0-4) 

`add_orphan` in `TxPoolService` holds no per-peer quota: [6](#0-5) 

`non_contextual_verify` (the only gate before orphan insertion) checks transaction structure but cannot check fee rate because input values are unknown for orphan transactions: [7](#0-6) 

When an orphan is evicted, `TxVerificationResult::Reject` is sent to the relayer, which marks the transaction as unknown in the bloom filter and does not re-request it — causing silent, permanent relay failure: [8](#0-7) 

## Impact Explanation
An attacker with a single P2P connection can saturate the entire orphan pool at zero CKB cost, causing legitimate orphan transactions from honest peers to be silently dropped and never re-requested. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attack is sustained at negligible cost (100 small transactions, re-submitted on eviction), and the relay layer's bloom filter suppression means affected transactions are permanently lost from relay without any error visible to the sender.

## Likelihood Explanation
The attack requires only a single inbound or outbound P2P connection. No CKB tokens are needed. The attacker crafts 100 transactions with random non-existent `OutPoint` inputs — these pass `non_contextual_verify` (valid structure) and fail contextual resolution with `OutPointError::Unknown`, triggering `add_orphan`. The orphan expiry time is `100 * MAX_BLOCK_INTERVAL`, so fake entries persist for a long time without reconnection. The attacker can monitor evictions via the `TxVerificationResult::Reject` signal and immediately re-submit to maintain saturation. Any unprivileged network peer can execute this. [9](#0-8) 

## Recommendation
1. **Per-peer orphan quota**: Track how many orphan slots each `PeerIndex` occupies in `OrphanPool`. When `limit_size()` fires, evict from the peer with the most entries first.
2. **Evict sender's own entry on overflow**: When the pool is full and a new orphan arrives from peer P, if P already holds entries, prefer evicting one of P's existing entries rather than an arbitrary victim.
3. **Minimum declared-cycle threshold**: Require a non-zero declared cycle count above a minimum threshold to raise the cost of fake orphan spam.
4. **Rate-limit orphan submissions per peer**: Track orphan submission rate per `PeerIndex` and ban peers that exceed a threshold within a time window (analogous to the existing `ban_malformed` mechanism).

## Proof of Concept
1. Establish a P2P connection to a target CKB node using the relay protocol.
2. Craft 100 transactions, each with one input referencing a random non-existent `OutPoint` (`tx_hash = random_bytes(32)`, `index = 0`), one output sending 61 CKB to any lock script, and a declared cycle count of 1.
3. Relay all 100 via `RelayTransactions` messages. Each passes `non_contextual_verify` (valid structure) and fails contextual resolution with `OutPointError::Unknown`, triggering `add_orphan` for each.
4. The orphan pool is now full with 100 attacker-controlled entries.
5. An honest peer relays a legitimate orphan transaction. It is inserted (pool size = 101), `limit_size()` fires, and one entry is evicted via `self.entries.keys().next()`. The legitimate transaction may be the evicted one.
6. The attacker receives `TxVerificationResult::Reject` for any evicted fake orphan and immediately re-submits it to maintain 100 slots.
7. Legitimate orphan transactions are continuously dropped; the relay layer marks them unknown via the bloom filter and does not re-request them, causing permanent silent relay failure.

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

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
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
