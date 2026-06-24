Audit Report

## Title
Orphan Transaction Pool Flooding via Unbounded Per-Peer Submissions Enables Targeted Transaction Eviction - (File: `tx-pool/src/component/orphan.rs`)

## Summary

`OrphanPool` enforces a global 100-slot cap (`DEFAULT_MAX_ORPHAN_TRANSACTIONS`) with no per-peer quota. When the pool is full, `limit_size()` evicts a pseudo-random entry by taking the first key from a `HashMap` iterator. An attacker peer can fill all 100 slots with fake orphan transactions referencing non-existent parents, then continuously replenish evicted slots to keep the pool saturated, causing legitimate orphan transactions to be evicted before their parents are confirmed.

## Finding Description

**Root cause — `OrphanPool::limit_size()` and `OrphanPool::add_orphan_tx()`**

`DEFAULT_MAX_ORPHAN_TRANSACTIONS` is a global constant of 100 slots shared across all peers with no per-peer accounting: [1](#0-0) 

When the pool exceeds 100 entries, `limit_size()` evicts by taking `self.entries.keys().next()` — effectively random for a `HashMap`, with no fee-rate priority, no age priority, and no per-peer fairness: [2](#0-1) 

`add_orphan_tx()` inserts any transaction whose `proposal_short_id` is not already present, then calls `limit_size()`. There is no check on how many entries the submitting peer already holds: [3](#0-2) 

The orphan pool explicitly permits multiple transactions spending the same unknown input, so the attacker can reuse a single fake parent hash across all flood entries at minimal construction cost: [4](#0-3) 

**Attack entry path**

When a submitted transaction fails with `is_missing_input`, it is unconditionally placed into the orphan pool with no further gate: [5](#0-4) 

**Relay-level filter and bypass**

`TransactionsProcess::execute()` filters incoming transactions against `unknown_tx_hashes`, requiring the node to have previously requested the transaction from that peer. However, this is bypassable: the attacker first sends `RelayTransactionHashes` announcing fake tx hashes, the node responds with `GetRelayTransactions`, and the attacker then delivers the fake orphan transactions — satisfying the filter. The rate limiter (30 req/s per peer per message type) only slows the initial fill; multiple hashes can be batched in a single `RelayTransactionHashes` message: [6](#0-5) [7](#0-6) 

**Promotion path bypassed on eviction**

When a parent is confirmed, `process_orphan_tx` searches the orphan pool for children to promote. If the legitimate orphan was evicted, it is silently absent and never promoted: [8](#0-7) 

## Impact Explanation

This is a **bad design which could cause CKB network congestion with few costs** (High, 10001–15000 points). An unprivileged attacker with a single P2P connection can suppress legitimate orphan transactions from being processed on a targeted node at negligible cost (no CKB tokens required, only CPU). If applied to multiple nodes simultaneously, this degrades the network's ability to relay and confirm child transactions, constituting a low-cost liveness attack on transaction propagation.

## Likelihood Explanation

- **Attacker preconditions:** A single unprivileged P2P peer connection; no keys, stake, or privileged access required.
- **Cost:** Constructing 100 minimal transactions with fake parent hashes costs only CPU time; no CKB tokens are spent.
- **Bypass of relay filter:** Announcing fake hashes via `RelayTransactionHashes` causes the node to request them, satisfying the `unknown_tx_hashes` guard in `TransactionsProcess`.
- **Sustainability:** After initial fill, the attacker replenishes each evicted slot by repeating the announce-request-send cycle, keeping the pool saturated indefinitely.
- **Targeting:** The attacker can observe a victim's orphan hash via relay gossip and time the flood to coincide with the victim's submission.

## Recommendation

1. **Add per-peer orphan slot quota.** Track how many orphan entries each `PeerIndex` holds in `OrphanPool`. Reject or preferentially evict entries from peers exceeding a per-peer cap (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`).

2. **Replace random eviction with fee-rate-weighted or peer-fairness eviction.** When the pool is full, identify the peer with the most entries and evict one of their entries before accepting the new one. The main pool already uses `EvictKey` (fee_rate, timestamp, descendants_count) for ordered eviction: [9](#0-8) 

3. **Limit `RelayTransactionHashes` batch size per peer** to reduce the attacker's ability to rapidly seed `unknown_tx_hashes` with fake entries.

## Proof of Concept

```
1. Attacker connects to a CKB node as a relay peer (SupportProtocols::RelayV3).

2. Attacker sends one RelayTransactionHashes message containing 100 random fake tx hashes.
   - Node adds all 100 to unknown_tx_hashes and sends GetRelayTransactions back.

3. Attacker responds with RelayTransactions containing 100 transactions T_a[0..99]:
   - Each T_a[i] has one input referencing a random, non-existent OutPoint.
   - Each T_a[i] has one output returning value to the attacker's own lock.
   - Declared cycle = 1 (minimal).

4. Each T_a[i] passes the unknown_tx_hashes filter (node requested them),
   fails with is_missing_input, and is placed in OrphanPool.
   After 100 submissions: OrphanPool.len() == 100 == DEFAULT_MAX_ORPHAN_TRANSACTIONS.

5. Honest user relays their legitimate orphan T_h (child of an unconfirmed parent P).
   - T_h is inserted; limit_size() fires and evicts one random entry.
   - Probability T_h is evicted on first round: 1/101 ≈ 1%.

6. Attacker announces a new fake hash, waits for GetRelayTransactions, sends T_a[100].
   - If T_h survived round 1, it now competes with 100 attacker entries again.
   - Attacker repeats; within seconds T_h is evicted with high probability.

7. Honest user's parent P is confirmed on-chain.
   - process_orphan_tx(P) fires but T_h is absent from the orphan pool.
   - T_h is never promoted to pending; user must detect and resubmit.
```

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

**File:** tx-pool/src/component/tests/orphan.rs (L29-44)
```rust
fn test_orphan_allows_double_spends_of_unknown_input() {
    let parent = build_tx(vec![(&Byte32::zero(), 1)], 1);
    let parent_hash = parent.hash();
    let tx1 = build_tx(vec![(&parent_hash, 0)], 1);
    let tx2 = build_tx(vec![(&parent_hash, 0)], 2);
    let mut orphan = OrphanPool::new();

    orphan.add_orphan_tx(tx1.clone(), 0.into(), 0);
    orphan.add_orphan_tx(tx2.clone(), 0.into(), 0);

    assert_eq!(orphan.len(), 2);
    let txs = orphan.find_by_previous(&parent);
    assert_eq!(txs.len(), 2);
    assert!(txs.contains(&&tx1.proposal_short_id()));
    assert!(txs.contains(&&tx2.proposal_short_id()));
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

**File:** tx-pool/src/process.rs (L591-600)
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
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/transactions_process.rs (L49-55)
```rust
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
```

**File:** tx-pool/src/component/sort_key.rs (L79-103)
```rust
#[derive(Eq, PartialEq, Clone, Debug)]
pub struct EvictKey {
    pub fee_rate: FeeRate,
    pub timestamp: u64,
    pub descendants_count: usize,
}

impl PartialOrd for EvictKey {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
```
