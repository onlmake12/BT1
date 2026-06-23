### Title
Missing Cell-Liveness Check in `proposed_rtx`/`gap_rtx` Allows Stale-Input Transactions to Be Promoted to `Proposed` State During Reorg — (File: `tx-pool/src/pool.rs`)

---

### Summary

During a chain reorganization, `_update_tx_pool_for_reorg` promotes pending/gap transactions to `Gap` or `Proposed` status by calling `gap_rtx` and `proposed_rtx`. Neither function re-validates whether the transaction's input cells are still live against the updated chain snapshot. This is the direct CKB analog of Futureswap's `addCollateral` operating on a closed trade without calling `ensureTradeOpen`: a state-modifying operation proceeds without confirming the target object is in a valid state.

---

### Finding Description

**Vulnerability class:** tx-pool state-transition without state validation.

**Root cause — `proposed_rtx` and `gap_rtx` in `tx-pool/src/pool.rs`:**

```rust
// tx-pool/src/pool.rs  lines 386-422
pub(crate) fn gap_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
    match self.get_pool_entry(short_id) {
        Some(entry) => {
            let tx_hash = entry.inner.transaction().hash();
            if entry.status == Status::Gap {
                Err(Reject::Duplicated(tx_hash))
            } else {
                self.set_entry_gap(short_id);   // ← status changed, no liveness check
                Ok(())
            }
        }
        ...
    }
}

pub(crate) fn proposed_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
    match self.get_pool_entry(short_id) {
        Some(entry) => {
            let tx_hash = entry.inner.transaction().hash();
            if entry.status == Status::Proposed {
                Err(Reject::Duplicated(tx_hash))
            } else {
                self.set_entry_proposed(short_id); // ← status changed, no liveness check
                Ok(())
            }
        }
        ...
    }
}
``` [1](#0-0) 

Both functions only guard against the "already in target status" duplicate case. They never call `check_rtx_from_pool`, which is the function that validates input-cell liveness against the overlay of pool state and the chain snapshot.

**Contrast with `submit_entry`, which does re-validate on tip change:**

```rust
// tx-pool/src/process.rs  lines 119-133
if pre_resolve_tip != tip_hash {
    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;
    ...
    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
}
``` [2](#0-1) 

The code is explicitly aware that a tip change invalidates prior resolution, yet the reorg promotion path skips this check entirely.

**Call site — `_update_tx_pool_for_reorg` in `tx-pool/src/process.rs`:**

```rust
// lines 1048-1106
tx_pool.snapshot = Arc::clone(&snapshot);          // snapshot updated first
tx_pool.remove_committed_txs(...);                 // removes direct conflicts
tx_pool.remove_by_detached_proposal(...);

// mine mode: promote without re-checking liveness
for (id, entry) in proposals {
    if let Err(e) = tx_pool.proposed_rtx(&id) { ... }
}
for (id, entry) in gaps {
    if let Err(e) = tx_pool.gap_rtx(&id) { ... }
}
``` [3](#0-2) 

The snapshot is updated to the new chain tip **before** the promotion loop, so `check_rtx_from_pool` would correctly reflect the post-reorg cell state if it were called — but it is not.

**Why `remove_committed_txs` does not close the gap:**

`remove_committed_txs` calls `remove_committed_tx` → `pool_map.resolve_conflict(tx)` for every tx in the attached blocks. This removes pool entries whose inputs directly collide with a committed tx's inputs. [4](#0-3) 

However, it does **not** remove pool entries whose inputs are outputs of a *detached* tx that subsequently fails to be re-added to the pool. The re-addition of detached txs (`readd_detached_tx`) happens **after** the promotion loop:

```
_update_tx_pool_for_reorg  (promotes at step 4)
readd_detached_tx           (re-adds detached txs at step 5)
``` [5](#0-4) 

If a detached tx D (whose output Y is consumed by pool tx A) cannot be re-added because D's own inputs are now dead in the new chain, then Y is absent from both the snapshot and the pool at promotion time. `proposed_rtx` still promotes A to `Proposed` with an unresolvable input.

**`check_rtx_from_pool` — the missing guard:**

```rust
// tx-pool/src/pool.rs  lines 363-370
pub(crate) fn check_rtx_from_pool(&self, rtx: &ResolvedTransaction) -> Result<(), Reject> {
    let snapshot = self.snapshot();
    let pool_cell = PoolCell::new(&self.pool_map, false);
    let checker = OverlayCellChecker::new(&pool_cell, snapshot);
    let mut seen_inputs = HashSet::new();
    rtx.check(&mut seen_inputs, &checker, snapshot)
        .map_err(Reject::Resolve)
}
``` [6](#0-5) 

This function, if called inside `proposed_rtx`/`gap_rtx`, would detect dead or unknown inputs against the already-updated snapshot and reject the promotion.

---

### Impact Explanation

A pool transaction with inputs that are dead or unknown in the post-reorg chain state is promoted to `Proposed` status. Consequences:

1. **Inconsistent pool state:** The tx-pool advertises a `Proposed` transaction that cannot be committed. Any RPC caller querying pool status receives misleading data.
2. **Block template pollution:** The block assembler draws from `Proposed` entries. The stale tx will be included in block templates, forcing the assembler or downstream validation to discard it — wasting CPU cycles on every template rebuild until the tx expires.
3. **Relay amplification:** The `call_proposed` callback fires, potentially relaying the stale tx to peers, who then waste resources attempting to validate or re-relay it.
4. **Persistent pool corruption:** The tx remains in `Proposed` state until `remove_expired` evicts it (governed by `expiry_hours`), which can be hours. During this window the pool is in a state the protocol does not intend.

---

### Likelihood Explanation

The trigger requires:
1. A chain reorg (achievable by any miner who produces a longer chain, or by a network partition).
2. At least one pool transaction whose input cell was created by a tx that is detached in the reorg and cannot be re-added to the pool (because its own inputs are now spent in the new chain).

Reorgs of 1–2 blocks are routine on any PoW chain. The second condition is satisfied whenever a detached block contains a tx that creates a UTXO consumed by a pending pool tx, and the new chain spends the same UTXO differently. This is a realistic scenario during competitive mining or network splits. No privileged access is required; any block-producing peer can trigger it.

---

### Recommendation

Inside `proposed_rtx` and `gap_rtx`, call `check_rtx_from_pool` on the entry's resolved transaction before calling `set_entry_proposed`/`set_entry_gap`. If the check fails, reject the promotion and invoke the reject callback (as already done for the `Err` branch). This mirrors the pattern already used in `submit_entry` when the tip changes.

```rust
pub(crate) fn proposed_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
    match self.get_pool_entry(short_id) {
        Some(entry) => {
            let tx_hash = entry.inner.transaction().hash();
            if entry.status == Status::Proposed {
                return Err(Reject::Duplicated(tx_hash));
            }
            // NEW: re-validate cell liveness against updated snapshot
            self.check_rtx_from_pool(&entry.inner.rtx)?;
            self.set_entry_proposed(short_id);
            Ok(())
        }
        None => Err(Reject::Malformed(...)),
    }
}
```

Apply the same pattern to `gap_rtx`.

---

### Proof of Concept

**Setup:**

1. Node is in mine mode (`block_assembler` configured).
2. Chain tip is block B₀. Pool contains tx **A** (status `Pending`) that spends output `X` of tx **D**, where D was committed in B₀.
3. A competing chain produces blocks B₁, B₂ that do not include D but do include a tx **E** that spends D's input (making D's output `X` permanently absent from the new canonical chain). B₁/B₂ have higher total difficulty.

**Trigger:**

4. Node receives B₁ and B₂, triggering a reorg. `update_tx_pool_for_reorg` is called.
5. `_update_tx_pool_for_reorg` updates the snapshot to B₂.
6. `remove_committed_txs` iterates over txs in B₁/B₂. D is not among them (D is detached, not attached). `resolve_conflict` is not called for D. **A is not removed.**
7. `readd_detached_tx` attempts to re-add D. D's inputs are now dead (spent by E in the new chain). D fails re-admission. Output `X` is absent from both snapshot and pool.
8. The promotion loop calls `tx_pool.proposed_rtx(&A.short_id)`. The function finds A, sees `status != Proposed`, and calls `set_entry_proposed` — **no liveness check performed**.

**Observable result:**

9. A is now in `Proposed` state. `get_transaction` RPC returns `status: proposed`.
10. `get_block_template` includes A in the template's transaction list.
11. Any attempt to submit a block containing A fails consensus validation (`OutPointError::Unknown` or `OutPointError::Dead` for input `X`).
12. A remains in `Proposed` state until `expiry_hours` elapses. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

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

**File:** tx-pool/src/pool.rs (L363-370)
```rust
    pub(crate) fn check_rtx_from_pool(&self, rtx: &ResolvedTransaction) -> Result<(), Reject> {
        let snapshot = self.snapshot();
        let pool_cell = PoolCell::new(&self.pool_map, false);
        let checker = OverlayCellChecker::new(&pool_cell, snapshot);
        let mut seen_inputs = HashSet::new();
        rtx.check(&mut seen_inputs, &checker, snapshot)
            .map_err(Reject::Resolve)
    }
```

**File:** tx-pool/src/pool.rs (L386-422)
```rust
    pub(crate) fn gap_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
        match self.get_pool_entry(short_id) {
            Some(entry) => {
                let tx_hash = entry.inner.transaction().hash();
                if entry.status == Status::Gap {
                    Err(Reject::Duplicated(tx_hash))
                } else {
                    debug!("gap_rtx: {:?} => {:?}", tx_hash, short_id);
                    self.set_entry_gap(short_id);
                    Ok(())
                }
            }
            None => Err(Reject::Malformed(
                String::from("invalid short_id"),
                Default::default(),
            )),
        }
    }

    pub(crate) fn proposed_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
        match self.get_pool_entry(short_id) {
            Some(entry) => {
                let tx_hash = entry.inner.transaction().hash();
                if entry.status == Status::Proposed {
                    Err(Reject::Duplicated(tx_hash))
                } else {
                    debug!("proposed_rtx: {:?} => {:?}", tx_hash, short_id);
                    self.set_entry_proposed(short_id);
                    Ok(())
                }
            }
            None => Err(Reject::Malformed(
                String::from("invalid short_id"),
                Default::default(),
            )),
        }
    }
```

**File:** tx-pool/src/process.rs (L119-134)
```rust
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }
```

**File:** tx-pool/src/process.rs (L836-851)
```rust
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }
```

**File:** tx-pool/src/process.rs (L1048-1106)
```rust
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
```
