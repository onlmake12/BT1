### Title
Tx-Pool Eviction Timestamp Ordering Bug Enables Low-Cost Griefing of Legitimate Pending Transactions — (File: `tx-pool/src/component/sort_key.rs`)

---

### Summary

The `EvictKey` comparator in `tx-pool/src/component/sort_key.rs` uses **ascending** timestamp ordering when evicting transactions with equal fee rates, meaning the **oldest** transaction is evicted first. The code comment explicitly states the opposite intent: "select the **latest** timestamp" for eviction. This inversion allows an attacker to fill the tx-pool with minimum-fee-rate transactions submitted **after** a legitimate user's transaction, causing the legitimate user's older transaction to be evicted while the attacker's newer transactions remain in the pool.

---

### Finding Description

The `EvictKey` struct is used to determine which transaction to remove when the pool exceeds `max_tx_pool_size`. Its `Ord` implementation is:

```rust
// tx-pool/src/component/sort_key.rs
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)   // ← ascending: OLDEST evicted first
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
}
```

The comment directly above the struct definition states:

> "First compare fee_rate, select the smallest fee_rate, and then select the **latest** timestamp, for eviction, the latest timestamp which also means that the fewer descendants may exist."

`self.timestamp.cmp(&other.timestamp)` produces ascending order. In `next_evict_entry`, `iter_by_evict_key()` iterates ascending and takes the **first** element — the entry with the **smallest** (oldest) timestamp. This is confirmed by the unit test `test_min_timestamp_evict`, which asserts the sorted eviction order is `[30, 31, 32]` (oldest first), and by `test_pool_evict`, which confirms `tx1` (added first, oldest timestamp) is always evicted first when all entries share the same fee rate.

The correct implementation to match the stated intent would be:
```rust
other.timestamp.cmp(&self.timestamp)  // descending: NEWEST evicted first
```

The `limit_size` function calls `next_evict_entry` in a loop after every new submission:

```rust
// tx-pool/src/pool.rs
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    if let Some(id) = next_evict_entry() {
        let removed = self.pool_map.remove_entry_and_descendants(&id);
        ...
    }
}
```

And `submit_entry` calls `limit_size` immediately after inserting each new transaction:

```rust
// tx-pool/src/process.rs
tx_pool
    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
    .map_or(Ok(()), Err)?;
```

**Concrete attack scenario:**

1. Alice submits `TX_A` with the minimum fee rate (`min_fee_rate = 1000 shannons/KB`). It enters the pool with timestamp `T_A`.
2. The attacker (Bob) fills the remaining pool capacity with minimum-fee-rate transactions, all submitted **after** Alice, so all have timestamps `T_B > T_A`.
3. The pool is now at `max_tx_pool_size`. Bob submits one additional transaction.
4. `limit_size` is triggered. It calls `next_evict_entry(Status::Pending)`, which iterates by ascending `EvictKey`. All transactions share the same fee rate and have no descendants. The oldest timestamp wins eviction — that is `TX_A` (Alice's).
5. Alice's transaction is evicted. Bob's transactions remain.
6. Bob can repeat step 3 every time Alice resubmits, at the cost of a single transaction fee per eviction cycle.

---

### Impact Explanation

A tx-pool submitter (unprivileged attacker) can persistently evict any specific pending transaction that was submitted before theirs, as long as both share the same fee rate tier. The victim's transaction is silently dropped with a `Reject::Full` error. The victim must resubmit at a higher fee rate or accept indefinite delay. With the default pool size of 180 MB and `min_fee_rate = 1000 shannons/KB`, the one-time cost to fill the pool is approximately 1.8 CKB; the ongoing per-eviction cost is a single minimum-fee transaction. This matches the external report's class: a bounded resource (pool size) can be occupied at low cost to block legitimate participants.

---

### Likelihood Explanation

The default `max_tx_pool_size` is 180 MB. Filling it requires capital and fees, but the cost is bounded and predictable. The attack is most effective during periods of low network activity when the pool is not already full of high-fee-rate transactions. Any unprivileged tx-pool submitter reachable via RPC (`send_transaction`) or P2P relay can execute this attack. No privileged access, key material, or majority hashpower is required.

---

### Recommendation

Fix the timestamp comparison in `EvictKey::cmp` to match the stated intent — evict the **newest** transaction first (descending order):

```rust
// tx-pool/src/component/sort_key.rs
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                // Fix: evict newest first (descending), protecting older txs with more descendants
                other.timestamp.cmp(&self.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
}
```

This ensures that when fee rates are equal, the most recently submitted transaction (which is least likely to have accumulated descendants) is evicted first, protecting older legitimate transactions.

---

### Proof of Concept

**Root cause confirmed by unit test** (`tx-pool/src/component/tests/entry.rs`):

```rust
// test_min_timestamp_evict: all same fee_rate (500/10), same descendants_count (0)
let mut result = vec![(500, 10, 30), (500, 10, 31), (500, 10, 32)]
    .into_iter()
    .map(|(fee, weight, timestamp)| EvictKey {
        fee_rate: FeeRate::calculate(Capacity::shannons(fee), weight),
        timestamp,
        descendants_count: 0,
    })
    .collect::<Vec<_>>();
result.sort();
// Actual result: [30, 31, 32] — OLDEST (30) is first, i.e., evicted first
assert_eq!(
    result.iter().map(|key| key.timestamp).collect::<Vec<_>>(),
    vec![30, 31, 32]
);
```

The test confirms the oldest timestamp is evicted first. The comment on `EvictKey` says the latest should be evicted first. The discrepancy is the root cause.

**Eviction path** (`tx-pool/src/pool.rs` → `tx-pool/src/component/pool_map.rs` → `tx-pool/src/component/sort_key.rs`): [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

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

**File:** tx-pool/src/process.rs (L149-152)
```rust
                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/component/tests/entry.rs (L21-36)
```rust
#[test]
fn test_min_timestamp_evict() {
    let mut result = vec![(500, 10, 30), (500, 10, 31), (500, 10, 32)]
        .into_iter()
        .map(|(fee, weight, timestamp)| EvictKey {
            fee_rate: FeeRate::calculate(Capacity::shannons(fee), weight),
            timestamp,
            descendants_count: 0,
        })
        .collect::<Vec<_>>();
    result.sort();
    assert_eq!(
        result.iter().map(|key| key.timestamp).collect::<Vec<_>>(),
        vec![30, 31, 32]
    );
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-20)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
