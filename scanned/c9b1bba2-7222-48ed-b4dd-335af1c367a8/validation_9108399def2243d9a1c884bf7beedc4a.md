### Title
Status-Priority Eviction in `limit_size` Allows Low-Fee Proposed Transactions to Survive While High-Fee Pending Transactions Are Evicted — (`tx-pool/src/pool.rs`)

---

### Summary

`TxPool::limit_size` uses a hard-coded status-priority eviction order (Pending → Gap → Proposed) rather than a global fee-rate ordering. As a result, any Pending transaction — regardless of how high its fee rate is — will be evicted before any Proposed transaction — regardless of how low its fee rate is. An unprivileged attacker who pre-fills the pool with low-fee transactions that have been promoted to `Status::Proposed` can cause a victim's high-fee `Status::Pending` transaction to be evicted on pool overflow, reducing miner revenue.

---

### Finding Description

**Root cause — `limit_size` in `tx-pool/src/pool.rs`:**

```rust
let next_evict_entry = || {
    self.pool_map
        .next_evict_entry(Status::Pending)
        .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
        .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
};
``` [1](#0-0) 

The `.or_else` chain means: if **any** Pending entry exists, it is chosen for eviction before **any** Proposed entry is even considered. The fee rate of the Pending entry is irrelevant to this decision.

**`next_evict_entry` correctly sorts within a status by fee rate:**

```rust
pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
    self.entries
        .iter_by_evict_key()
        .find(move |entry| entry.status == status)
        .map(|entry| entry.id.clone())
}
``` [2](#0-1) 

`iter_by_evict_key()` iterates in ascending `EvictKey` order (lowest fee rate first). Within a given status this is correct. But the status filter applied in `limit_size` means the global minimum fee-rate entry is never selected — only the minimum within the first non-empty status bucket.

**`EvictKey` ordering (fee rate primary):**

```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate { ... }
        else { self.fee_rate.cmp(&other.fee_rate) }
    }
}
``` [3](#0-2) 

The `EvictKey` design is correct in isolation — it would produce the globally lowest fee-rate entry if queried without a status filter. The bug is that `limit_size` never queries across statuses.

**`EvictKey` construction uses descendants fee rate:**

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
    }
}
``` [4](#0-3) 

An attacker's low-fee Proposed txs with no descendants will have a low `fee_rate` in their `EvictKey`, making them the globally correct eviction candidates — but they are never reached because Pending entries are always drained first.

---

### Impact Explanation

When the pool is at capacity and a victim submits a high-fee Pending transaction:

1. The tx is inserted; `total_tx_size > max_tx_pool_size`.
2. `limit_size` is called with `current_entry_id = Some(victim_tx_id)`.
3. `next_evict_entry(Status::Pending)` returns the victim's tx (the only or lowest-fee Pending entry).
4. The victim's high-fee tx is removed and `Reject::Full` is returned.
5. The attacker's low-fee Proposed txs remain in the pool and are committed by miners at low revenue.

Miner revenue is damaged: the high-fee tx that would have been committed is replaced by low-fee txs. The victim's tx is permanently rejected from this node's pool.

---

### Likelihood Explanation

The attack requires no special privileges:

- The attacker submits many transactions at `min_fee_rate` via the standard P2P/RPC submission path.
- Miners routinely include all pending transactions in their proposal zones to maximize future revenue, so the attacker's txs naturally transition to `Status::Proposed` within one proposal window (~2 blocks).
- Once Proposed, they are shielded from eviction as long as any Pending tx exists.
- The attacker only needs to keep the pool near `max_tx_pool_size` (default ~180 MB) with Proposed txs, which is achievable with many small transactions.

No hashpower, no leaked keys, no privileged access is required.

---

### Recommendation

Replace the status-priority selection in `limit_size` with a single global fee-rate scan across all statuses:

```rust
// Instead of:
self.pool_map.next_evict_entry(Status::Pending)
    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))

// Use a cross-status minimum:
self.pool_map.entries
    .iter_by_evict_key()   // already sorted ascending by fee_rate
    .next()
    .map(|e| e.id.clone())
```

This preserves the existing `EvictKey` ordering (lowest fee rate, then latest timestamp, then fewest descendants) while removing the status bias. Proposed transactions that genuinely have the lowest fee rate will then be evicted first, protecting high-fee Pending transactions and preserving miner revenue.

---

### Proof of Concept

Using the existing `plug_entry` / `add_entry` internal API (as used in the existing test suite in `tx-pool/src/component/tests/pending.rs`):

```rust
#[test]
fn test_limit_size_evicts_high_fee_pending_before_low_fee_proposed() {
    let mut pool = PoolMap::new(1000);

    // Attacker: low-fee tx, gets proposed
    let attacker_tx = build_tx(vec![(&h256!("0x1").into(), 0)], 1);
    let attacker_entry = TxEntry::dummy_resolve(
        attacker_tx.clone(), 2, Capacity::shannons(10), 100 // fee=10, size=100 → 0.1 shannon/byte
    );
    pool.add_entry(attacker_entry, Status::Proposed).unwrap();

    // Victim: high-fee tx, still pending
    let victim_tx = build_tx(vec![(&h256!("0x2").into(), 0)], 1);
    let victim_entry = TxEntry::dummy_resolve(
        victim_tx.clone(), 2, Capacity::shannons(10_000), 100 // fee=10000, size=100 → 100 shannon/byte
    );
    pool.add_entry(victim_entry, Status::Pending).unwrap();

    // Eviction candidate should be the attacker's low-fee Proposed tx,
    // but next_evict_entry(Pending) returns the victim's high-fee Pending tx instead.
    let evict_id = pool.next_evict_entry(Status::Pending)
        .or_else(|| pool.next_evict_entry(Status::Gap))
        .or_else(|| pool.next_evict_entry(Status::Proposed));

    // BUG: evict_id == victim_tx.proposal_short_id() (high-fee Pending evicted first)
    // CORRECT: evict_id should == attacker_tx.proposal_short_id() (low-fee Proposed evicted first)
    assert_ne!(evict_id, Some(victim_tx.proposal_short_id()),
        "High-fee Pending tx must not be evicted before low-fee Proposed tx");
}
``` [5](#0-4) [2](#0-1) [6](#0-5)

### Citations

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
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

**File:** tx-pool/src/component/sort_key.rs (L76-103)
```rust
/// First compare fee_rate, select the smallest fee_rate,
/// and then select the latest timestamp, for eviction,
/// the latest timestamp which also means that the fewer descendants may exist.
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

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```
