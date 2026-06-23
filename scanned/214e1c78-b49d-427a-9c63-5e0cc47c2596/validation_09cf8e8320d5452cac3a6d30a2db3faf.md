### Title
Incomplete Tie-Breaking in `AncestorsScoreSortKey::cmp` Causes Non-Deterministic Tx Ordering at Block-Capacity Boundary — (`File: tx-pool/src/component/sort_key.rs`)

---

### Summary

The `AncestorsScoreSortKey` comparator used to sort all transactions in the CKB tx-pool does not define a total order. When two distinct transactions produce an identical effective fee rate **and** an identical `ancestors_weight`, the comparator returns `Ordering::Equal`. Because the `PoolEntry` multi-index map uses `#[multi_index(ordered_non_unique)]` on this key, the relative position of equal-score entries is implementation-defined (insertion-order dependent). An unprivileged tx-pool submitter can craft a transaction that collides with a victim transaction's score, causing non-deterministic selection at the block-capacity boundary — analogous to the Celo `SortedLinkedList` arbitrary-ordering issue.

---

### Finding Description

In `tx-pool/src/component/sort_key.rs`, the `Ord` implementation for `AncestorsScoreSortKey` is:

```rust
impl Ord for AncestorsScoreSortKey {
    fn cmp(&self, other: &Self) -> Ordering {
        let (fee, weight) = self.min_fee_and_weight();
        let (other_fee, other_weight) = other.min_fee_and_weight();
        let self_weight  = u128::from(fee.as_u64())       * u128::from(other_weight);
        let other_weight = u128::from(other_fee.as_u64()) * u128::from(weight);
        if self_weight == other_weight {
            // if fee rate weight is same, then compare with ancestor weight
            self.ancestors_weight.cmp(&other.ancestors_weight)
        } else {
            self_weight.cmp(&other_weight)
        }
    }
}
```

When `self_weight == other_weight` **and** `self.ancestors_weight == other.ancestors_weight`, the function returns `Ordering::Equal` for two **distinct** transactions. This is not a total order. [1](#0-0) 

The `PoolEntry` struct registers `score: AncestorsScoreSortKey` as `#[multi_index(ordered_non_unique)]`: [2](#0-1) 

When two entries compare as `Equal`, the `multi_index_map` BTreeMap-backed ordered index places them in insertion order. This means the **first** transaction submitted with a given score occupies the "earlier" position in the sorted iteration.

This score index drives three critical operations:

1. **Block template transaction selection** — `TxSelector::txs_to_commit` iterates `sorted_proposed_iter()` (which calls `iter_by_score().rev()`) and stops when `size_limit` or `cycles_limit` is reached. Transactions at the boundary are selected or excluded based on their position in this order. [3](#0-2) 

2. **Proposal selection** — `get_proposals` takes the top `limit` pending transactions by score. Transactions at the `proposals_limit` boundary are included or excluded based on their position. [4](#0-3) 

3. **Fee rate estimation** — `estimate_fee_rate` iterates by score to simulate block filling; the boundary transaction's fee rate is returned as the estimate. [5](#0-4) 

The `get_transaction_weight` function is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For a standalone transaction (no ancestors), `ancestors_weight == weight`. Two transactions with the same serialized size and the same cycle count will have identical `AncestorsScoreSortKey` values, causing `cmp` to return `Equal`. [6](#0-5) 

By contrast, the `EvictKey` comparator **does** define a total order via a three-level tie-break: `fee_rate` → `descendants_count` → `timestamp` (millisecond-unique per entry). Eviction is therefore deterministic, but selection for inclusion is not. [7](#0-6) 

---

### Impact Explanation

**Block template construction** (`package_txs` → `TxSelector::txs_to_commit`) and **proposal packaging** (`package_proposals` → `get_proposals`) both depend on the score-sorted order to decide which transactions fall inside or outside the block/proposal capacity boundary. [8](#0-7) 

When the pool is near capacity and two transactions have equal `AncestorsScoreSortKey`, the one inserted **first** into the pool occupies the higher-priority position in the sorted iteration. The other transaction may be excluded from the block template or proposal set even though it has an identical fee rate. This creates:

- **Unfair exclusion**: A victim transaction with the same fee rate as an attacker's transaction may be consistently deprioritized if the attacker submits first.
- **Fee estimation distortion**: `estimate_fee_rate` returns the fee rate of the boundary transaction; if the boundary is occupied by an attacker-controlled equal-score transaction, the returned estimate is manipulated.
- **Non-deterministic block templates**: Different nodes with different pool insertion histories may produce different block templates for equal-score transactions at the boundary, reducing template consistency across the network.

---

### Likelihood Explanation

The conditions for exploitation are:

1. The attacker observes a target transaction's `fee`, `size`, and `cycles` (all visible via `get_raw_tx_pool` RPC or by monitoring relayed transactions).
2. The attacker crafts a transaction with the same `get_transaction_weight(size, cycles)` and the same `fee / weight` ratio. Since fees are in shannons and sizes are in bytes, exact matches are achievable by choosing appropriate output values.
3. The attacker submits their transaction **before** the victim's (to occupy the earlier position) or **after** (to displace the victim at the boundary on the next pool update).

The `send_transaction` RPC is open to any unprivileged caller. The attack requires no special privileges, no key material, and no majority hash power. The main constraint is that the pool must be near its `max_block_bytes` or `proposals_limit` boundary for the ordering to matter — a realistic condition during periods of high network activity.

---

### Recommendation

Add a deterministic tie-breaker to `AncestorsScoreSortKey::cmp` so that the comparator defines a strict total order over all distinct pool entries. The natural candidate is the transaction's `ProposalShortId` (a hash-derived identifier, unique per transaction). Alternatively, include the entry's `timestamp` (already present in `TxEntry`) as a secondary tie-breaker, consistent with the approach used in `EvictKey`. For example:

```rust
if self_weight == other_weight {
    if self.ancestors_weight == other.ancestors_weight {
        // final tie-break: use a unique per-entry field, e.g. timestamp
        // (requires passing it into the key, or using ProposalShortId)
        Ordering::Equal  // <-- replace this with a unique comparator
    } else {
        self.ancestors_weight.cmp(&other.ancestors_weight)
    }
}
```

The `EvictKey` pattern (which already uses `timestamp` as a final tie-breaker) should be replicated in `AncestorsScoreSortKey`. [9](#0-8) 

---

### Proof of Concept

1. Submit transaction **A**: `size = 200 bytes`, `cycles = 0`, `fee = 1000 shannons`. Weight = `max(200, 0) = 200`. Score key: `fee=1000, weight=200, ancestors_fee=1000, ancestors_weight=200`.

2. Submit transaction **B** (attacker): `size = 200 bytes`, `cycles = 0`, `fee = 1000 shannons`. Identical score key.

3. `AncestorsScoreSortKey::cmp(A, B)`:
   - `self_weight = 1000 * 200 = 200_000`
   - `other_weight = 1000 * 200 = 200_000`
   - `self_weight == other_weight` → compare `ancestors_weight`: `200 == 200` → returns `Ordering::Equal`.

4. The `multi_index_map` ordered index places A and B in insertion order. Whichever was inserted first appears first in `iter_by_score().rev()`.

5. Fill the pool to near `max_block_bytes`. Call `get_block_template`. The transaction inserted first is selected; the other is excluded — despite identical fee rates. The attacker controls which transaction is excluded by controlling submission timing. [10](#0-9) [11](#0-10)

### Citations

**File:** tx-pool/src/component/sort_key.rs (L36-50)
```rust
impl Ord for AncestorsScoreSortKey {
    fn cmp(&self, other: &Self) -> Ordering {
        // avoid division a_fee/a_weight > b_fee/b_weight
        let (fee, weight) = self.min_fee_and_weight();
        let (other_fee, other_weight) = other.min_fee_and_weight();
        let self_weight = u128::from(fee.as_u64()) * u128::from(other_weight);
        let other_weight = u128::from(other_fee.as_u64()) * u128::from(weight);
        if self_weight == other_weight {
            // if fee rate weight is same, then compare with ancestor weight
            self.ancestors_weight.cmp(&other.ancestors_weight)
        } else {
            self_weight.cmp(&other_weight)
        }
    }
}
```

**File:** tx-pool/src/component/sort_key.rs (L79-104)
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
}
```

**File:** tx-pool/src/component/pool_map.rs (L46-58)
```rust
#[derive(MultiIndexMap, Clone)]
pub struct PoolEntry {
    #[multi_index(hashed_unique)]
    pub id: ProposalShortId,
    #[multi_index(ordered_non_unique)]
    pub score: AncestorsScoreSortKey,
    #[multi_index(hashed_non_unique)]
    pub status: Status,
    #[multi_index(ordered_non_unique)]
    pub evict_key: EvictKey,
    // other sort key
    pub inner: TxEntry,
}
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```

**File:** tx-pool/src/component/pool_map.rs (L362-374)
```rust
    pub(crate) fn get_proposals(
        &self,
        limit: usize,
        exclusion: &HashSet<ProposalShortId>,
    ) -> HashSet<ProposalShortId> {
        self.score_sorted_iter_by_status(Status::Pending)
            .filter_map(|entry| {
                let id = entry.proposal_short_id();
                (!exclusion.contains(&id)).then_some(id)
            })
            .take(limit)
            .collect()
    }
```

**File:** tx-pool/src/component/tx_selector.rs (L106-162)
```rust
        let mut iter = self
            .pool_map
            .sorted_proposed_iter()
            .filter(|entry| {
                entry.ancestors_size <= size_limit && entry.ancestors_cycles <= cycles_limit
            })
            .peekable();
        loop {
            let mut using_modified = false;

            if let Some(entry) = iter.peek()
                && self.skip_proposed_entry(&entry.proposal_short_id())
            {
                iter.next();
                continue;
            }

            // First try to find a new transaction in `proposed_pool` to evaluate.
            let tx_entry: TxEntry = match (iter.peek(), self.modified_entries.next_best_entry()) {
                (Some(entry), Some(best_modified)) => {
                    if &best_modified > entry {
                        using_modified = true;
                        best_modified.clone()
                    } else {
                        // worse than `proposed_pool`
                        iter.next().cloned().expect("peek guard")
                    }
                }
                (Some(_), None) => {
                    // Either no entry in `modified_entries`
                    iter.next().cloned().expect("peek guarded")
                }
                (None, Some(best_modified)) => {
                    // We're out of entries in `proposed`; use the entry from `modified_entries`
                    using_modified = true;
                    best_modified.clone()
                }
                (None, None) => {
                    break;
                }
            };

            let short_id = tx_entry.proposal_short_id();
            let next_size = size.saturating_add(tx_entry.ancestors_size);
            let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

            if next_cycles > cycles_limit || next_size > size_limit {
                consecutive_failed += 1;
                if using_modified {
                    self.modified_entries.remove(&short_id);
                    self.failed_txs.insert(short_id.clone());
                }
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
            }
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/pool.rs (L524-554)
```rust
    pub(crate) fn package_proposals(
        &self,
        proposals_limit: u64,
        uncles: &[UncleBlockView],
    ) -> HashSet<ProposalShortId> {
        let uncle_proposals: HashSet<ProposalShortId> = uncles
            .iter()
            .flat_map(|u| u.data().proposals().into_iter())
            .collect();
        self.get_proposals(proposals_limit as usize, &uncle_proposals)
    }

    pub(crate) fn package_txs(
        &self,
        max_block_cycles: Cycle,
        txs_size_limit: usize,
    ) -> (Vec<TxEntry>, usize, Cycle) {
        let (entries, size, cycles) =
            TxSelector::new(&self.pool_map).txs_to_commit(txs_size_limit, max_block_cycles);

        if !entries.is_empty() {
            ckb_logger::info!(
                "[get_block_template] candidate txs count: {}, size: {}/{}, cycles:{}/{}",
                entries.len(),
                size,
                txs_size_limit,
                cycles,
                max_block_cycles
            );
        }
        (entries, size, cycles)
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```
