### Title
Tx-Pool Expiry Timer Never Resets After Proposal-Window Cycling, Incorrectly Marking Active Transactions as Rejected — (`tx-pool/src/pool.rs`)

---

### Summary

`TxEntry::timestamp` is set once at pool submission and never updated. When a proposed transaction's proposal window expires and it is moved back to `Pending` via `remove_by_detached_proposal`, the original timestamp is preserved. If this cycle repeats long enough, `remove_expired` fires based on the stale original timestamp and emits `Reject::Expiry`, recording the transaction in `recent_reject` and broadcasting a relay-rejection signal — even though the submitter actively and correctly attempted to get the transaction committed.

---

### Finding Description

`TxEntry` stores a `timestamp` field set exactly once at creation:

```rust
// tx-pool/src/component/entry.rs:48-49
pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
    Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
``` [1](#0-0) 

The expiry check in `remove_expired` compares this original timestamp against wall-clock time:

```rust
// tx-pool/src/pool.rs:277
.filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
``` [2](#0-1) 

When a transaction's proposal window expires (it was proposed but not committed within `w_far` blocks), `remove_by_detached_proposal` moves it back to `Pending`:

```rust
// tx-pool/src/pool.rs:345-348
for mut entry in entries {
    entry.reset_statistic_state();   // resets ancestor/descendant counts only
    let ret = self.add_pending(entry); // timestamp is NOT reset
``` [3](#0-2) 

`reset_statistic_state()` only resets ancestor/descendant accounting fields — it does not touch `timestamp`: [4](#0-3) 

The ordering in `_update_tx_pool_for_reorg` is: `remove_by_detached_proposal` → (re-propose) → `remove_expired`. So in the same reorg cycle, a tx can be moved back to pending and then immediately expired if the original timestamp is old enough: [5](#0-4) 

When `remove_expired` fires, it calls `callbacks.call_reject` with `Reject::Expiry`. The registered reject callback:

1. Records the tx in `recent_reject` (`should_recorded()` returns `true` for `Reject::Expiry` — it only exempts `Duplicated`)
2. Sends `TxVerificationResult::Reject` to the relay layer (`is_allowed_relay()` returns `true` for `Reject::Expiry`) [6](#0-5) [7](#0-6) 

---

### Impact Explanation

A transaction submitter who correctly submits a valid transaction, which is then proposed on-chain multiple times but not committed (due to block fullness or miner selection — not the submitter's fault), will have their transaction:

- Removed from the pool with `Reject::Expiry`
- Recorded as `Rejected` in the `recent_reject` database, visible via the `get_transaction` RPC as `Status::Rejected` with reason `"Expiry transaction, timestamp {T0}"`
- Broadcast as a relay rejection to peers, potentially causing the transaction to be deprioritized or not relayed by connected nodes
- Forced to be resubmitted

The submitter did nothing wrong — the expiry fires because the clock started at initial submission and was never reset across proposal-window cycles. This is the direct analog to the external report: a user is penalized (incorrect `Rejected` state + relay rejection) despite actively and correctly attempting the action.

**Impact: Medium** — Incorrect `Rejected` status and relay suppression for a legitimately active transaction; no direct fund loss, but the submitter's transaction is silently dropped and misclassified.

---

### Likelihood Explanation

**Likelihood: Medium** — Requires a transaction to remain in the pool across multiple proposal-window cycles for longer than `expiry_hours` (default 12 hours, configurable). This is realistic during sustained network congestion where blocks are consistently full and miners do not include the transaction within its proposal window. The default `expiry_hours = 12` means any tx that has been cycling for half a day is at risk. [8](#0-7) 

---

### Recommendation

Reset `entry.timestamp` to the current time when `remove_by_detached_proposal` moves a transaction back to `Pending`. This mirrors the intent of `reset_statistic_state()` — the entry is being re-admitted to the pending pool and should be treated as freshly re-entered for expiry purposes:

```rust
// In remove_by_detached_proposal, after reset_statistic_state():
entry.timestamp = ckb_systemtime::unix_time_as_millis();
entry.reset_statistic_state();
let ret = self.add_pending(entry);
```

Alternatively, track a separate `last_activity_timestamp` field in `TxEntry` and use that for expiry, so that any state transition (pending → proposed → pending) refreshes the clock. [9](#0-8) 

---

### Proof of Concept

1. Submit a valid transaction `tx` to the node via `send_transaction` RPC. Record `T0 = now`.
2. A miner proposes `tx` in a block (it moves to `Proposed` status in the pool).
3. The proposal window (`w_far` blocks, default 10) passes without `tx` being committed (e.g., blocks are full with higher-fee transactions).
4. On the next reorg/block, `remove_by_detached_proposal` moves `tx` back to `Pending` with `timestamp = T0` unchanged.
5. Steps 2–4 repeat. The submitter is actively trying; `tx` is being proposed but not committed.
6. After `expiry_hours` (12 hours by default) from `T0`, `remove_expired` fires: `expiry + T0 < now_ms` is true.
7. `tx` is removed with `Reject::Expiry(T0)`, written to `recent_reject`, and a relay rejection is sent.
8. Querying `get_transaction(tx_hash)` via RPC returns `{"status": "rejected", "reason": "Expiry transaction, timestamp ..."}` — even though the submitter did nothing wrong and the transaction was being actively proposed. [10](#0-9) [11](#0-10)

### Citations

**File:** tx-pool/src/component/entry.rs (L42-44)
```rust
    /// The unix timestamp when entering the Txpool, unit: Millisecond
    pub timestamp: u64,
}
```

**File:** tx-pool/src/component/entry.rs (L46-50)
```rust
impl TxEntry {
    /// Create new transaction pool entry
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }
```

**File:** tx-pool/src/component/entry.rs (L168-179)
```rust
    /// Reset ancestor state by remove
    pub fn reset_statistic_state(&mut self) {
        self.ancestors_count = 1;
        self.ancestors_size = self.size;
        self.ancestors_cycles = self.cycles;
        self.ancestors_fee = self.fee;

        self.descendants_count = 1;
        self.descendants_size = self.size;
        self.descendants_cycles = self.cycles;
        self.descendants_fee = self.fee;
    }
```

**File:** tx-pool/src/pool.rs (L270-288)
```rust
    // Expire all transaction (and their dependencies) in the pool.
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L333-356)
```rust
    pub(crate) fn remove_by_detached_proposal<'a>(
        &mut self,
        ids: impl Iterator<Item = &'a ProposalShortId>,
    ) {
        for id in ids {
            if let Some(e) = self.pool_map.get_by_id(id) {
                let status = e.status;
                if status == Status::Pending {
                    continue;
                }
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/process.rs (L1050-1113)
```rust
    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());

    // mine mode:
    // pending ---> gap ----> proposed
    // try move gap to proposed
    if mine_mode {
        let mut proposals = Vec::new();
        let mut gaps = Vec::new();

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
            let short_id = entry.inner.proposal_short_id();
            let elem = (short_id.clone(), entry.inner.clone());
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push(elem);
            } else if snapshot.proposals().contains_gap(&short_id) {
                gaps.push(elem);
            }
        }

        for (id, entry) in proposals {
            debug!("begin to proposed: {:x}", id);
            if let Err(e) = tx_pool.proposed_rtx(&id) {
                debug!(
                    "Failed to add proposed tx {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e);
            } else {
                callbacks.call_proposed(&entry)
            }
        }

        for (id, entry) in gaps {
            debug!("begin to gap: {:x}", id);
            if let Err(e) = tx_pool.gap_rtx(&id) {
                debug!(
                    "Failed to add tx to gap {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e.clone());
            }
        }
    }

    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** util/types/src/core/tx_pool.rs (L99-113)
```rust
    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }

    /// Returns true if tx can be resubmitted, allowing relay
    /// * Declared wrong cycles should allow relay with the correct cycles
    /// * Reject but is not malformed and the fee rate reached the threshold,
    ///   it may be due to double spending
    ///   or temporary limitations of the pool resources,
    ///   and expired clearing
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```

**File:** shared/src/shared_builder.rs (L576-601)
```rust
    tx_pool_builder.register_reject(Box::new(
        move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
            let tx_hash = entry.transaction().hash();
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }

            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }

            // notify
            let notify_tx_entry = create_notify_entry(entry);
            notify_reject.notify_reject_transaction(notify_tx_entry, reject);

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
```

**File:** util/app-config/src/legacy/tx_pool.rs (L17-18)
```rust
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
```
