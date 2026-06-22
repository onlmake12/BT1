### Title
Proposed Transactions Occupy Shared Pool Space Without Cross-Status Fee-Rate Eviction, Enabling Tx-Pool DoS Against New Submissions — (`File: tx-pool/src/pool.rs`)

---

### Summary

The CKB tx-pool enforces a single global size limit (`max_tx_pool_size`) across all transaction statuses (Pending, Gap, Proposed). When the pool is full, the `limit_size` eviction function always evicts Pending transactions before it will touch Proposed ones. Because `total_tx_size` counts every status together, an attacker who fills the pool with transactions that subsequently get proposed can permanently occupy pool space that is shielded from eviction, causing every new Pending submission to be immediately rejected with `Reject::Full`, regardless of its fee rate.

---

### Finding Description

`PoolMap` maintains a single `total_tx_size` counter that accumulates the serialized size of every entry regardless of its `Status` (Pending, Gap, or Proposed). [1](#0-0) 

When `total_tx_size` exceeds `max_tx_pool_size`, `TxPool::limit_size` is called. Its eviction loop unconditionally tries Pending first, then Gap, then Proposed: [2](#0-1) 

The eviction key within each status group selects the entry with the **lowest fee rate**: [3](#0-2) 

`limit_size` is invoked immediately after every successful insertion, with `current_entry_id` set to the just-inserted transaction: [4](#0-3) 

If the newly inserted Pending transaction is the only Pending entry and the pool is already full of Proposed transactions, `limit_size` selects and evicts it, returning `Reject::Full` to the caller. The Proposed transactions are never touched.

The default pool size is 180 MB: [5](#0-4) 

There is no separate capacity budget for the Proposed sub-pool; the single `max_tx_pool_size` field is the only knob: [6](#0-5) 

---

### Impact Explanation

Once the pool is saturated with Proposed transactions, every subsequent `send_transaction` RPC call or P2P relay submission is rejected with `Reject::Full`. Legitimate users with high-fee transactions cannot enter the pool because the eviction logic will always evict the newly inserted Pending entry before it will evict any lower-fee Proposed entry. This is a complete, sustained denial-of-service against transaction submission for the duration the Proposed transactions remain uncommitted (up to the default 12-hour expiry). [7](#0-6) 

---

### Likelihood Explanation

The attack is cheap and requires no privileged access:

1. The attacker submits minimum-fee transactions via the public `send_transaction` RPC or P2P relay. At the default minimum fee rate of 1 000 shannons/KB and a 180 MB pool, filling the pool costs approximately 1.8 CKB in fees.
2. Miners naturally propose these transactions (they are valid and in the Pending pool). Once proposed, they transition to Gap/Proposed status and become shielded from eviction.
3. The attacker continuously re-submits new minimum-fee transactions to replace those that eventually get committed, sustaining the DoS.

The entry path is fully unprivileged: any `send_transaction` caller or P2P peer can trigger this. [8](#0-7) 

---

### Recommendation

Decouple the eviction logic from status priority. Instead of always evicting Pending before Proposed, select the globally lowest-fee-rate entry across all statuses. Alternatively, impose a separate size cap on the Proposed sub-pool so that Proposed transactions cannot consume more than a bounded fraction of `max_tx_pool_size`, preserving headroom for new Pending submissions regardless of how many transactions have been proposed.

---

### Proof of Concept

1. Fill the pool with `N` minimum-fee transactions via `send_transaction` (N chosen so their total serialized size approaches `max_tx_pool_size`).
2. Wait for miners to propose them; they transition from `Status::Pending` to `Status::Gap`/`Status::Proposed` via `set_entry_gap` / `set_entry_proposed`.
3. Submit a new transaction with a fee rate well above `min_fee_rate`.
4. `submit_entry` inserts it as Pending, then calls `limit_size(…, Some(&entry.proposal_short_id()))`.
5. `limit_size` finds `total_tx_size > max_tx_pool_size`, calls `next_evict_entry(Status::Pending)`, which returns the just-inserted high-fee transaction (the only Pending entry), evicts it, and returns `Reject::Full`.
6. The caller receives `Reject::Full` even though the pool contains many lower-fee Proposed transactions that were never considered for eviction. [9](#0-8) [3](#0-2)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L60-75)
```rust
pub struct PoolMap {
    /// The pool entries with different kinds of sort strategies
    pub(crate) entries: MultiIndexPoolEntryMap,
    /// All the deps, header_deps, inputs, outputs relationships
    pub(crate) edges: Edges,
    /// All the parent/children relationships
    pub(crate) links: TxLinksMap,
    pub(crate) max_ancestors_count: usize,
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
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

**File:** tx-pool/src/process.rs (L149-153)
```rust
                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-18)
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
```

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-13)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```
