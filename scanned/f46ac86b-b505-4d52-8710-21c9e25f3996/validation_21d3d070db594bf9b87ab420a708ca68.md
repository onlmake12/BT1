### Title
Orphan Transactions Permanently Stuck in Tx-Pool When Parent Cell Is Spent On-Chain — (`File: tx-pool/src/component/orphan.rs`)

---

### Summary

When a committed block spends a cell that an orphan transaction is waiting for, the orphan transaction is never evicted from the orphan pool. The `remove_committed_tx` path cleans up only the `pool_map` (pending/proposed pool) via `resolve_conflict`, but performs no corresponding cleanup of the `OrphanPool`. The orphan entry remains permanently stuck — its missing input can never be satisfied — until the time-based expiry fires or random eviction displaces it. An unprivileged peer can exploit this to exhaust all 100 orphan pool slots, causing legitimate orphan transactions to be randomly evicted.

---

### Finding Description

The `OrphanPool` in `tx-pool/src/component/orphan.rs` stores transactions whose inputs reference cells not yet present in the chain or pool. It maintains a secondary index `by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>` mapping each missing input outpoint to the set of orphan transactions waiting for it. [1](#0-0) 

Orphan transactions are promoted to the pending pool when `find_by_previous` is called after a new transaction is accepted into `pool_map`: [2](#0-1) 

However, when a block is committed, `remove_committed_txs` iterates over committed transactions and calls `remove_committed_tx` for each: [3](#0-2) 

`remove_committed_tx` calls `pool_map.resolve_conflict(tx)` to evict conflicting entries from the pending/proposed pool: [4](#0-3) 

**There is no corresponding call to clean up the `OrphanPool`.** If a committed transaction spends a cell `O` that an orphan transaction `T_orphan` is waiting for (i.e., `T_orphan`'s missing parent `T_parent` would have consumed `O`), `T_orphan` remains in the orphan pool indefinitely. Its `by_out_point` entry for the now-dead cell is never removed. `T_orphan` can never be promoted because its prerequisite chain is permanently broken.

The only cleanup mechanisms for the orphan pool are time-based expiry and random eviction when the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`: [5](#0-4) 

The expiry window is `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL`, which is a substantial wall-clock duration: [6](#0-5) 

The reorg handler `_update_tx_pool_for_reorg` confirms that no orphan pool cleanup is triggered on block attachment: [7](#0-6) 

---

### Impact Explanation

An attacker who controls 100 live cells can fill all 100 orphan pool slots with permanently-stuck orphan transactions. Once full, `limit_size` evicts entries **randomly** (`self.entries.keys().next()`), meaning legitimate orphan transactions submitted by honest peers are displaced with no priority ordering: [8](#0-7) 

Consequences:
- Legitimate orphan transactions from honest peers are randomly evicted, preventing them from being promoted to pending and eventually confirmed.
- The stuck orphans occupy pool slots for the full `ORPHAN_TX_EXPIRE_TIME` window before natural expiry.
- The attack is repeatable: as slots expire, the attacker can refill them with new stuck orphans using fresh UTXOs.

---

### Likelihood Explanation

The attack requires an unprivileged peer to submit orphan transactions via the P2P relay protocol — a standard, supported entry path. The attacker needs 100 live cells (UTXOs) to fill the pool, which is a low but non-zero cost. The attack is fully deterministic: the attacker controls both the orphan transaction submission and the competing confirmed transaction that kills the parent cell. No privileged access, key leakage, or majority hashpower is required.

---

### Recommendation

In `remove_committed_tx` (or in `_update_tx_pool_for_reorg`), after calling `pool_map.resolve_conflict(tx)` for each committed transaction, also scan the `OrphanPool`'s `by_out_point` index for any orphan transactions whose missing inputs reference outputs of the committed transaction (i.e., cells that are now permanently dead because the transaction that would have created them can never be valid). Evict those orphan entries immediately rather than waiting for time-based expiry.

Concretely: for each output `O` of a committed transaction `T_committed`, check `orphan_pool.by_out_point.get(O)` and call `remove_orphan_txs` on the result.

---

### Proof of Concept

1. Attacker owns live cell `A`.
2. Attacker constructs `T_parent` (not submitted): spends `A`, produces output `B`.
3. Attacker constructs `T_orphan`: spends `B` (missing input). Submits `T_orphan` via P2P relay → goes to orphan pool, indexed under `B` in `by_out_point`.
4. Attacker submits `T_other`: spends `A` (same cell as `T_parent` would have), gets confirmed in a block.
5. Node calls `remove_committed_tx(T_other)` → `pool_map.resolve_conflict(T_other)` → no orphan pool cleanup.
6. `T_orphan` remains in the orphan pool. `B` can never exist (its creator `T_parent` is now invalid). `T_orphan` is permanently stuck.
7. Repeat steps 1–6 with 100 different cells to fill all orphan pool slots.
8. Any subsequent legitimate orphan transaction submitted by an honest peer triggers `limit_size`, which randomly evicts one of the 100 stuck entries **or** the new legitimate entry — with equal probability — preventing honest orphan transactions from being retained. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
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

**File:** tx-pool/src/component/orphan.rs (L161-167)
```rust
    pub fn find_by_previous(&self, tx: &TransactionView) -> Vec<&ProposalShortId> {
        tx.output_pts()
            .iter()
            .filter_map(|out_point| self.by_out_point.get(out_point))
            .flatten()
            .collect::<Vec<_>>()
    }
```

**File:** tx-pool/src/pool.rs (L223-241)
```rust
    pub(crate) fn remove_committed_txs<'a>(
        &mut self,
        txs: impl Iterator<Item = &'a TransactionView>,
        callbacks: &Callbacks,
        detached_headers: &HashSet<Byte32>,
    ) {
        for tx in txs {
            let tx_hash = tx.hash();
            debug!("try remove_committed_tx {}", tx_hash);
            self.remove_committed_tx(tx, callbacks);

            self.committed_txs_hash_cache
                .put(tx.proposal_short_id(), tx_hash);
        }

        if !detached_headers.is_empty() {
            self.resolve_conflict_header_dep(detached_headers, callbacks)
        }
    }
```

**File:** tx-pool/src/pool.rs (L253-268)
```rust
    fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
        let short_id = tx.proposal_short_id();
        if let Some(_entry) = self.pool_map.remove_entry(&short_id) {
            debug!("remove_committed_tx for {}", tx.hash());
        }
        {
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
            }
        }
    }
```

**File:** tx-pool/src/process.rs (L1039-1057)
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

```
