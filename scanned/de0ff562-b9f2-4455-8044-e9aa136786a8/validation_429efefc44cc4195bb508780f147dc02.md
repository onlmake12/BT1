### Title
Orphan Pool Evicts Transactions Randomly Instead of by Fee Rate, Enabling DoS via Pool Saturation - (File: `tx-pool/src/component/orphan.rs`)

### Summary
The `OrphanPool` in CKB evicts transactions randomly when the pool reaches its hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. Because there is no fee-rate-based eviction strategy, an unprivileged P2P peer can saturate the orphan pool with 100 low-fee (or zero-fee) orphan transactions and continuously re-submit any that get evicted, preventing legitimate orphan transactions from other users from being retained and later promoted to the pending pool.

### Finding Description
In `tx-pool/src/component/orphan.rs`, the `limit_size()` function is called every time a new orphan transaction is added via `add_orphan_tx()`. When the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS` (100), the overflow eviction loop at lines 119–125 selects the victim using `self.entries.keys().next()` — a `HashMap` iterator that yields entries in an arbitrary, non-fee-ordered sequence:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

The `OrphanPool::Entry` struct stores a `cycle` field (declared cycles from the peer) but this value is never consulted during eviction. There is no `EvictKey`, no fee-rate ordering, and no per-peer slot accounting. Any transaction — regardless of fee — has an equal probability of being evicted once the pool is full.

The entry path from the network is:
1. A remote peer sends a `RelayTransactions` P2P message.
2. `TransactionsProcess::execute()` calls `tx_pool.submit_remote_tx(tx, declared_cycles, peer)`.
3. `resumeble_process_tx()` attempts verification; if inputs are missing it calls `self.add_orphan(tx, peer, declared_cycle)`.
4. `add_orphan()` calls `orphan.add_orphan_tx()`, which calls `limit_size()` with random eviction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Impact Explanation
When the orphan pool is saturated, every new legitimate orphan transaction submitted by an honest user triggers a random eviction. The attacker, who controls 100 slots, has a ~100/101 probability of surviving each eviction round. By monitoring the relay network for `TxVerificationResult::Reject` notifications and immediately re-submitting evicted transactions, the attacker can maintain near-total pool occupancy indefinitely.

Orphan transactions that are evicted are marked as rejected and removed from the relay filter, meaning the node will not re-request them from peers. When the parent transaction later arrives and `process_orphan_tx()` is called, the evicted child transaction is no longer present and will not be promoted to the pending pool. This silently drops valid transactions from the mempool, degrading transaction relay reliability and potentially preventing inclusion in blocks. [6](#0-5) [7](#0-6) 

### Likelihood Explanation
The attack requires only a single P2P connection and the ability to craft 100 transactions with missing parent inputs — a trivial capability for any network participant. The orphan pool limit of 100 is small enough that saturation is achievable with minimal resources. No privileged access, hashpower, or key material is required. The attacker does not need to pay fees since orphan transactions are not verified for fee rate before being admitted to the orphan pool (fee verification happens only after the parent is resolved). [1](#0-0) [8](#0-7) 

### Recommendation
Replace the random eviction in `limit_size()` with a fee-rate-ordered eviction strategy:

1. **Store fee rate at admission**: When `add_orphan_tx()` is called, record the declared fee rate (derivable from the declared cycles and transaction size) in the `Entry` struct.
2. **Evict lowest fee-rate entry**: In `limit_size()`, instead of `self.entries.keys().next()`, iterate to find the entry with the lowest declared fee rate and evict it. This mirrors the `EvictKey`-based strategy already used in `PoolMap::next_evict_entry()`.
3. **Per-peer slot limiting**: Cap the number of orphan slots any single peer can occupy (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / 4`) to prevent a single peer from monopolizing the pool regardless of fee rate. [9](#0-8) [10](#0-9) 

### Proof of Concept
1. Attacker connects to a CKB node as a P2P peer via the relay protocol.
2. Attacker generates 100 transactions (`spam_0` … `spam_99`) each spending a non-existent output (e.g., a random 32-byte tx hash as parent). These are valid in structure but have missing inputs, so they will be classified as orphans.
3. Attacker sends all 100 via `RelayTransactions` messages. The node admits them into the orphan pool, which is now at capacity (`len == 100`).
4. Victim user sends a legitimate orphan transaction `victim_tx` (e.g., a child of a parent currently in transit on the network).
5. The node calls `add_orphan_tx(victim_tx, ...)` → `limit_size()` → randomly evicts one entry. With probability 100/101 ≈ 99%, the evicted entry is one of the attacker's spam transactions.
6. The attacker monitors `TxVerificationResult::Reject` callbacks and immediately re-submits the evicted spam transaction, restoring the pool to 100 attacker-controlled entries.
7. `victim_tx` is evicted on the next insertion cycle. When `victim_tx`'s parent later arrives and `process_orphan_tx()` is called, `victim_tx` is absent from the orphan pool and is silently lost. [11](#0-10) [12](#0-11)

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

**File:** tx-pool/src/process.rs (L318-353)
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

    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
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

**File:** sync/src/relayer/transactions_process.rs (L85-93)
```rust
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });
```

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
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
