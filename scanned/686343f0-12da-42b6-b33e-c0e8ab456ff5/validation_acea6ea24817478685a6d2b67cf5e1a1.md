### Title
Verify Queue Admits Transactions Without Fee-Rate Gating and Evicts Nothing, Enabling Congestion-Based Exclusion of Time-Sensitive Transactions — (File: `tx-pool/src/component/verify_queue.rs`)

---

### Summary

CKB's `VerifyQueue` — the mandatory pre-pool staging area every submitted transaction must pass through before entering the `PoolMap` — accepts transactions in strict FIFO order by arrival time, performs no fee-rate check at admission, and has no eviction mechanism. When the queue reaches its hard 256 MB cap, all new submissions are rejected with `Reject::Full` regardless of fee rate. An unprivileged attacker who floods the queue with large, low-fee transactions can delay or permanently exclude legitimate time-sensitive transactions from ever reaching the pool and being included in a block.

---

### Finding Description

**Root cause — `VerifyQueue::add_tx()` (lines 198–237, `tx-pool/src/component/verify_queue.rs`)**

```rust
pub fn add_tx(
    &mut self,
    tx: TransactionView,
    is_proposal_tx: bool,
    remote: Option<(Cycle, PeerIndex)>,
) -> Result<bool, Reject> {
    // ...
    let tx_size = tx.data().serialized_size_in_block();
    // ...
    if self.is_full(tx_size) {
        return Err(Reject::Full(format!(
            "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
            tx.hash()
        )));
    }
    self.inner.insert(VerifyEntry {
        id: tx.proposal_short_id(),
        added_time: unix_time_as_millis(),   // ← only ordering key
        // ...
    });
```

No fee-rate check is performed before insertion. The only admission gate is the 256 MB hard cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`). Once the cap is hit, every subsequent submission — regardless of how high its fee rate is — is rejected.

**Root cause — `VerifyQueue::peek()` (lines 180–194)**

```rust
pub fn peek(&self, only_small_cycle: bool) -> Option<ProposalShortId> {
    let mut iter = self.inner.iter_by_added_time();   // ← FIFO

    if let Some(proposal_entry) = iter.find(|e| e.is_proposal_tx) {
        return Some(proposal_entry.inner.tx.proposal_short_id());
    }
    // falls back to FIFO for all other txs
    let entry = if only_small_cycle {
        self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
    } else {
        self.inner.iter_by_added_time().next()
    };
    entry.map(|e| e.inner.tx.proposal_short_id())
}
```

The only special-cased priority is `is_proposal_tx` (transactions already proposed in a prior block). All other transactions — including high-fee, time-sensitive ones — are dequeued in arrival order. There is no fee-rate index on the verify queue, and no eviction of low-fee entries to make room for high-fee arrivals.

**Contrast with downstream pool — `PoolMap` eviction**

Once a transaction clears the verify queue and enters the `PoolMap`, it is sorted and evicted by `EvictKey` (lowest fee rate first). The `TxSelector` then selects transactions for block templates purely by `AncestorsScoreSortKey` (package fee rate). The fee-rate discipline that exists in the pool is entirely absent at the verify-queue stage, which is the only admission bottleneck.

**Supporting evidence — `required` field permanently unimplemented**

```rust
// tx-pool/src/block_assembler/mod.rs line 985-993
pub(crate) fn tx_entry_to_template(entry: &TxEntry) -> TransactionTemplate {
    TransactionTemplate {
        hash: entry.transaction().hash().into(),
        required: false, // unimplemented
        // ...
    }
}
```

The `TransactionTemplate.required` field — which the CKB RPC spec defines to allow marking a transaction as mandatory for block inclusion — is hardcoded to `false` for every transaction. Even if a transaction reaches the pool and the block template, miners are never instructed to treat any transaction as required, compounding the prioritization gap.

---

### Impact Explanation

CKB transactions can carry `since` fields that encode absolute or relative time-lock conditions (block height or median-time-past). A transaction whose `since` window expires before it is verified and proposed becomes permanently invalid. An attacker who fills the 256 MB verify queue with large, low-fee transactions forces all subsequent submissions into a `Reject::Full` error. Transactions that were already in the queue are processed in FIFO order, so even if the victim's transaction entered the queue before the cap was hit, it may sit behind thousands of attacker transactions and miss its validity window. Because the `required` field is also unimplemented, there is no fallback mechanism at the block-template layer to guarantee inclusion of any transaction the node operator considers critical.

---

### Likelihood Explanation

The verify queue cap is 256 MB. CKB's `TRANSACTION_SIZE_LIMIT` is on the order of hundreds of kilobytes per transaction, meaning a few hundred to a few thousand transactions are sufficient to saturate the queue. An attacker with modest resources can submit this volume via the public `send_transaction` RPC or via P2P relay without any privileged access. The attack is amplified during organic network congestion, when the queue is already partially full. No cryptographic material, majority hash power, or insider access is required.

---

### Recommendation

1. **Fee-rate gating at verify-queue admission**: Before inserting into `VerifyQueue`, check the transaction's declared fee rate against `min_fee_rate`. Transactions below the threshold should be rejected immediately rather than occupying queue space.

2. **Fee-rate based eviction in `VerifyQueue`**: When the queue is full and a new transaction with a higher fee rate arrives, evict the lowest-fee-rate entry to make room, mirroring the eviction logic already present in `PoolMap`.

3. **Implement `TransactionTemplate.required`**: Provide a mechanism — operator-configurable or protocol-defined — to mark specific transactions as required in block templates, so miners are instructed to include them unconditionally.

---

### Proof of Concept

1. Attacker opens connections to a CKB node via RPC or P2P.
2. Attacker submits a stream of large transactions (e.g., transactions with large witness blobs, each near the per-transaction size limit) with fees at or just above zero. Each call to `VerifyQueue::add_tx()` succeeds until `total_tx_size` approaches 256 MB.
3. Victim submits a time-sensitive transaction (e.g., one with a `since` field expiring in N blocks). `VerifyQueue::add_tx()` returns `Err(Reject::Full(...))` — the transaction never enters the verification pipeline.
4. Alternatively, if the victim's transaction entered the queue before the cap was hit, `VerifyQueue::peek()` returns attacker transactions first (earlier `added_time`), so the victim's transaction waits behind all attacker transactions. Verify workers drain the attacker's transactions (rejecting them for low fees), but by the time the victim's transaction is dequeued and verified, the `since` window has passed and the transaction is invalid.
5. The victim's transaction is never proposed, never committed, and the time-sensitive operation it encodes (e.g., unlocking a time-locked cell) is permanently blocked for that window. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L180-194)
```rust
    pub fn peek(&self, only_small_cycle: bool) -> Option<ProposalShortId> {
        let mut iter = self.inner.iter_by_added_time();

        if let Some(proposal_entry) = iter.find(|e| e.is_proposal_tx) {
            return Some(proposal_entry.inner.tx.proposal_short_id());
        }

        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };

        entry.map(|e| e.inner.tx.proposal_short_id())
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-237)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L985-993)
```rust
pub(crate) fn tx_entry_to_template(entry: &TxEntry) -> TransactionTemplate {
    TransactionTemplate {
        hash: entry.transaction().hash().into(),
        required: false, // unimplemented
        cycles: Some(entry.cycles.into()),
        depends: None, // unimplemented
        data: entry.transaction().data().into(),
    }
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

**File:** tx-pool/src/component/tx_selector.rs (L52-65)
```rust
/// Selects transactions for inclusion in a block-template using **package-aware** fee-rate sorting.
///
/// ### Package definition
/// A package is a connected group of ≤ MAX_ANCESTORS_COUNT（1_000）transactions
/// The mempool is linearly ordered into non-overlapping packages using a greedy clustering
/// algorithm that maximizes total fee for a given size and cycles.
///
/// ### Why packages instead of individual transactions?
/// - A high-fee child transaction is worthless without its low-fee parent(s) (CPFP).
/// - A low-fee parent with many high-fee children should be prioritized as a unit (package).
/// - Sorting individual txs breaks incentive compatibility and leads to suboptimal templates.
///
/// ### Sorting rule
/// Packages are sorted by **package fee rate** = total fee / total weight of the entire package.
```
