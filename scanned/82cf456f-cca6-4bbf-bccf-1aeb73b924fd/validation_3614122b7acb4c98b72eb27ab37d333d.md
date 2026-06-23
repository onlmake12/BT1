### Title
Unbounded O(N) Iteration Over All Tx-Pool Entries on Every Block Processing — (`tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

---

### Summary

On every new tip block, the CKB tx-pool service performs two separate full-pool scans: one unconditional expiry scan (`remove_expired`) and one status-promotion scan over all pending/gap entries (inside `_update_tx_pool_for_reorg`). The pool enforces a **byte-size** ceiling (`max_tx_pool_size`) but no **entry-count** ceiling. An unprivileged tx-pool submitter can flood the pool with many minimum-size transactions, making each block-processing cycle O(N) in the number of pool entries — a direct analog of the Skale "iterations over slashes" resource-exhaustion class.

---

### Finding Description

Two separate iteration pipelines execute on every call to `_update_tx_pool_for_reorg`, which is invoked for every new tip block:

**Pipeline 1 — `remove_expired` (unconditional, always runs):**

`tx-pool/src/pool.rs:271-288` calls `self.pool_map.iter()` to scan every entry in the pool and collect those whose timestamp has expired:

```rust
let removed: Vec<_> = self
    .pool_map
    .iter()
    .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
    .map(|entry| entry.inner.clone())
    .collect();
```

This is an unconditional O(N) scan over all pool entries, regardless of how many are actually expired. [1](#0-0) 

**Pipeline 2 — status-promotion scan (mine mode):**

`tx-pool/src/process.rs:1065-1080` iterates over every `Status::Gap` entry and every `Status::Pending` entry to decide whether each should be promoted to `Proposed` or `Gap` status:

```rust
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) { ... }
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) { ... }
```

This is a second O(N) scan over all pending/gap entries on every block. [2](#0-1) 

Both pipelines are called from `_update_tx_pool_for_reorg`, which is the single reorg handler invoked on every new block: [3](#0-2) 

**Root cause — no entry-count ceiling:**

The pool enforces only a byte-size ceiling via `max_tx_pool_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size { ... }
``` [4](#0-3) 

There is no separate limit on the number of entries. A CKB transaction can be as small as ~61 bytes. With a default `max_tx_pool_size` of 180 MB, an attacker can create approximately **3 million pool entries**, each of which is visited by both pipelines on every block.

---

### Impact Explanation

- Mining nodes and full nodes with mine mode enabled spend O(N) CPU time per block on pool maintenance, where N is attacker-controlled.
- With a saturated pool, block-processing latency increases linearly, potentially causing the node to fall behind the chain tip, miss mining windows, or become unresponsive to RPC callers.
- The two separate iteration pipelines compound the cost: each block triggers at minimum two full O(N) passes.
- This is a resource-accounting / tx-pool admission issue reachable by any unprivileged `send_transaction` RPC caller.

---

### Likelihood Explanation

- Any unprivileged RPC caller can submit transactions via `send_transaction`.
- The attacker only needs to pay the minimum fee rate for each transaction; the pool holds them until expiry (default expiry is hours).
- No special privileges, leaked keys, majority hashpower, or social engineering are required.
- The attack is cheap relative to the CPU cost imposed on the victim node.

---

### Recommendation

1. **Enforce a hard entry-count cap** in addition to the byte-size cap inside `limit_size` (`tx-pool/src/pool.rs`), analogous to Bitcoin Core's `maxmempool` entry limit.
2. **Merge the two iteration pipelines** (expiry scan + status-promotion scan) into a single pass to halve the per-block cost, directly mirroring the Skale recommendation to merge the two slash-processing pipelines.
3. **Replace the full expiry scan** with a min-heap or sorted structure ordered by expiry timestamp, so only actually-expired entries are visited rather than the entire pool.

---

### Proof of Concept

1. Connect to a CKB node's RPC endpoint.
2. Submit ~N minimum-size, minimum-fee-rate transactions (each spending a distinct live cell) until `total_tx_size` approaches `max_tx_pool_size`.
3. Observe that each new block causes the node to execute `remove_expired` (full pool scan, `tx-pool/src/pool.rs:274-279`) and the gap/pending promotion loops (`tx-pool/src/process.rs:1065-1080`).
4. Monitor block-processing latency via the `get_tip_block_number` RPC; it will increase as N grows, demonstrating the O(N)-per-block cost imposed by the attacker-controlled pool size.

### Citations

**File:** tx-pool/src/pool.rs (L271-288)
```rust
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

**File:** tx-pool/src/process.rs (L1039-1114)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

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
}
```
