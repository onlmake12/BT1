### Title
Orphan Transaction Pool Flooding via Unbounded Per-Peer Submissions Enables Targeted Transaction Eviction - (File: `tx-pool/src/component/orphan.rs`)

---

### Summary

The `OrphanPool` in CKB's tx-pool accepts orphan transactions from any peer with no per-peer submission cap and evicts entries randomly when the pool reaches its hard limit of 100 slots. An attacker peer can fill all 100 slots with fake orphan transactions referencing non-existent parents, then continuously replenish evicted slots to keep the pool saturated. Any legitimate orphan transaction submitted by an honest user is then subject to random eviction, mirroring the FaultDisputeGame pattern where an attacker opens many parallel subgames at the same level to exhaust the honest defender's finite resource budget.

---

### Finding Description

**Root cause — `OrphanPool::limit_size()` and `OrphanPool::add_orphan_tx()`**

`DEFAULT_MAX_ORPHAN_TRANSACTIONS` is a global constant of 100 slots shared across all peers. [1](#0-0) 

When the pool is full, `limit_size()` evicts a random entry by taking the first key from the `HashMap` iterator — there is no fee-rate priority, no age priority, and no per-peer accounting: [2](#0-1) 

`add_orphan_tx()` inserts any transaction whose `proposal_short_id` is not already present, then calls `limit_size()`. There is no check on how many entries the submitting peer already has in the pool: [3](#0-2) 

The orphan pool also explicitly permits multiple transactions spending the same unknown input (double-spends of an unresolved parent), so the attacker can reuse a single fake parent hash across all 100 flood entries at minimal construction cost: [4](#0-3) 

**Attack flow (analog to FaultDisputeGame multi-subgame exhaustion):**

| FaultDisputeGame | CKB OrphanPool |
|---|---|
| Attacker opens N subgames at same tree level | Attacker submits N fake orphans referencing non-existent parents |
| Honest defender must counter each subgame | Honest user's orphan competes for the same 100 slots |
| Defender runs out of bond funds | Pool is saturated; honest orphan is randomly evicted |
| One uncountered subgame wins for attacker | Honest transaction is silently dropped; attacker refills the slot |

The relay-level rate limiter (`30 req/s per peer per message type`) only slows the initial fill to ~3.4 seconds; after that the attacker sustains saturation by replacing each evicted fake orphan at the same rate: [5](#0-4) 

The `add_orphan` call path in `after_process` shows that any remote peer whose transaction resolves to `is_missing_input` will have its transaction placed into the orphan pool with no further gate: [6](#0-5) 

---

### Impact Explanation

1. **Transaction delay / liveness failure.** A legitimate user's child transaction (orphan) is evicted before its parent is confirmed. The node marks the transaction as `Reject` to the relayer filter, so the peer that relayed it stops retrying. The user must detect the eviction and resubmit.

2. **Time-sensitive transaction loss.** CKB uses a two-phase proposal/commit window. If an orphan is evicted and the user fails to resubmit before the proposal window closes, the transaction misses its commit window. For time-locked contracts (e.g., DAO withdrawals, HTLC expiry, UDT settlement) this can cause permanent loss of the locked value.

3. **Sustained suppression.** Because the attacker can maintain pool saturation indefinitely at negligible cost (no bond, no fee required for orphan submission), the honest user's transaction can be suppressed for an arbitrarily long period.

---

### Likelihood Explanation

- **Attacker preconditions:** A single unprivileged P2P peer connection. No keys, no stake, no privileged access.
- **Cost:** Constructing 100 minimal transactions with fake parent hashes costs only CPU time; no CKB tokens are spent because orphan transactions are never broadcast to miners or committed on-chain.
- **Sustainability:** The relay rate limiter (30/s) is sufficient to replenish evicted slots faster than a legitimate user can resubmit, keeping the pool saturated.
- **Targeting:** The attacker can observe the victim's orphan hash via the relay gossip protocol and time the flood to coincide with the victim's submission.

---

### Recommendation

1. **Add per-peer orphan slot quota.** Track how many orphan entries each `PeerIndex` holds. Reject or preferentially evict entries from peers that exceed a per-peer cap (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`).

2. **Replace random eviction with fee-rate-weighted eviction.** The main pool already uses `EvictKey` (fee_rate, timestamp, descendants_count) for eviction ordering. Apply the same logic to the orphan pool so that zero-fee flood entries are evicted first. [7](#0-6) 

3. **Evict from the largest contributing peer first.** When the pool is full, identify the peer with the most entries and evict one of their entries before accepting the new one. This directly mirrors the per-participant fairness fix needed in the FaultDisputeGame context.

---

### Proof of Concept

```
1. Attacker connects to a CKB node as a relay peer (SupportProtocols::RelayV3).

2. Attacker constructs 100 transactions T_a[0..99]:
   - Each T_a[i] has one input referencing a random, non-existent OutPoint
     (tx_hash = random_bytes(32), index = 0).
   - Each T_a[i] has one output returning value to the attacker's own lock.
   - Declared cycle = 1 (minimal).

3. Attacker relays all 100 T_a[i] via RelayTransactions messages.
   - Each is rejected by the node with `is_missing_input` → placed in OrphanPool.
   - After 100 submissions, OrphanPool.len() == 100 == DEFAULT_MAX_ORPHAN_TRANSACTIONS.

4. Honest user relays their legitimate orphan T_h (child of an unconfirmed parent P).
   - T_h is inserted; limit_size() fires and evicts one random entry.
   - Probability T_h is evicted on first round: 1/101 ≈ 1%.

5. Attacker immediately submits T_a[100] (a new fake orphan) to refill the evicted slot.
   - If T_h survived round 1, it now competes with 100 attacker entries again.
   - Expected rounds until T_h is evicted: ~101, but each round takes <1 second.

6. Attacker repeats step 5 continuously.
   - Within seconds, T_h is evicted with high probability.
   - The node sends TxVerificationResult::Reject for T_h to the relayer filter,
     marking it as "unknown" and stopping relay retries.

7. Honest user's parent P is confirmed on-chain.
   - process_orphan_tx(P) fires, but T_h is no longer in the orphan pool.
   - T_h is never promoted to pending; the user must detect and resubmit.

8. If T_h is time-sensitive (e.g., must be proposed within N blocks of P's confirmation),
   sustained suppression causes the user to miss the window and lose locked funds.
```

The eviction path is confirmed at: [8](#0-7) 

The promotion path that is bypassed when T_h is absent: [9](#0-8)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L96-132)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
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

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
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

**File:** tx-pool/src/process.rs (L591-641)
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
                        orphan.tx.hash(),
                        tx.hash(),
                    );
                    let orphan_id = orphan.tx.proposal_short_id();
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
                        }
                        Err(reject) => {
                            warn!(
                                "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );
                        }
                    }
                } else if let Some((ret, _snapshot)) = self
                    ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
                    .await
                {
                    match ret {
                        Ok(_) => {
                            self.send_result_to_relayer(TxVerificationResult::Ok {
                                original_peer: Some(orphan.peer),
                                tx_hash: orphan.tx.hash(),
                            });
                            debug!(
                                "process_orphan {} success, find previous from {}",
                                orphan.tx.hash(),
                                tx.hash()
                            );
                            self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                            orphan_queue.push_back(orphan.tx);
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
