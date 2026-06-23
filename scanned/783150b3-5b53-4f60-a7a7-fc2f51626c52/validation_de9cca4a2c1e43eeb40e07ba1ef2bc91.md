### Title
TOCTOU Race in Async TX-Pool Verification Admits Dead-Input Transactions to Pool — (File: `tx-pool/src/process.rs`)

---

### Summary

The CKB tx-pool uses an async two-phase verification pipeline. Cell liveness is checked during `pre_check` under a read lock, but is **not re-validated** during `_submit_entry` under a write lock. A block committed between these two phases can spend cells that were resolved as live during `pre_check`, causing transactions with dead inputs to be admitted to the pending pool — an exact structural analog to the validator-exit / delegator-registration TOCTOU described in the external report.

---

### Finding Description

The tx-pool admission pipeline in `tx-pool/src/process.rs` operates in three sequential phases:

**Phase 1 — `pre_check` (read lock held):**

`pre_check` acquires a read lock on the pool, calls `resolve_tx` which resolves all input cells against the current chain `Snapshot`, checks pool-internal consistency via `check_rtx_from_pool`, captures the `tip_hash`, and then **releases the read lock**. [1](#0-0) 

**Phase 2 — Async CKB-VM script verification (no lock held):**

After `pre_check` returns, the resolved transaction (`rtx`) is passed to the script verifier. This can take an arbitrarily long time (bounded only by cycle limits). **No lock is held during this window.**

**Phase 3 — `_submit_entry` (write lock held):**

After verification completes, `_submit_entry` is called. It calls `tx_pool.add_pending(entry)` → `pool_map.add_entry(entry, Status::Pending)`. [2](#0-1) 

The `add_entry` path checks only **pool-internal** edge conflicts — cells consumed by other pool transactions — via `PoolCell`: [3](#0-2) 

`PoolCell::cell()` returns `CellStatus::Dead` only if the outpoint is tracked in `pool_map.edges` (i.e., consumed by another pool transaction). It returns `CellStatus::Unknown` — not `Dead` — for cells that are spent **in the chain** but not in the pool. This means chain-level cell liveness is **not re-checked** at submission time. [4](#0-3) 

**The TOCTOU window:**

Between Phase 1 and Phase 3, a new block can be committed that spends a cell `C` which was resolved as live during `pre_check`. The pool's snapshot is updated by `_update_tx_pool_for_reorg`, which removes committed and conflicting transactions **already in the pool**: [5](#0-4) 

However, a transaction currently in async verification is **not yet in the pool**, so `remove_committed_txs` cannot evict it. After verification completes, `_submit_entry` adds it to the pool with a dead input — the pool's `Status::Pending` entry now references a cell already spent on-chain.

The three-state `Status` enum for pool entries has no "stale" or "invalid" state: [6](#0-5) 

---

### Impact Explanation

Transactions admitted with dead inputs can never be committed. They occupy pool capacity (counted against `max_tx_pool_size`) and consume `total_tx_size` / `total_tx_cycles` accounting. An attacker who repeatedly triggers this condition can exhaust pool capacity, causing legitimate pending transactions to be evicted by the size-limit eviction path: [7](#0-6) 

This is a pool-level resource exhaustion / DoS. Unlike the external report where user funds are locked in a contract, here the user's on-chain funds are not locked (the cell is already spent), but the pool is polluted with permanently-uncommittable entries.

---

### Likelihood Explanation

An unprivileged RPC caller (`send_transaction`) is the entry point. The attacker submits transaction T1 with a computationally expensive lock script (maximizing verification time) spending cell C. Concurrently, the attacker submits a simpler, higher-fee transaction T2 also spending C. Miners preferentially include T2 (higher fee). If T2 is committed before T1's verification completes, T1 is admitted to the pool with a dead input. This is repeatable and requires no mining power, no privileged access, and no social engineering — only the ability to submit transactions via RPC.

---

### Recommendation

In `_submit_entry` (or immediately before it), re-validate input cell liveness against the **current** pool snapshot. If `tx_pool.snapshot.tip_hash()` differs from the `tip_hash` captured during `pre_check`, either re-resolve the transaction or reject it with `Reject::Resolve(OutPointError::Dead(...))`. Alternatively, add a chain-level liveness check inside `pool_map.add_entry` that consults the current snapshot for any input not found in the pool's edge map.

---

### Proof of Concept

1. Attacker crafts T1: spends cell C, uses a lock script that consumes near-maximum cycles (long verification time). Submits via `send_transaction` RPC.
2. T1 passes `pre_check`: C is live in snapshot S1, read lock released, async verification begins.
3. Attacker submits T2: also spends C, minimal script, higher fee. T2 is fast-verified and enters the pending pool.
4. A miner includes T2 in block B. `_update_tx_pool_for_reorg` runs: T2 is removed from pool, snapshot advances to S2 (C is now dead on-chain). T1 is not in the pool yet, so it is not evicted.
5. T1's verification completes. `_submit_entry` calls `pool_map.add_entry(T1, Pending)`. `PoolCell::cell(C)` returns `Unknown` (C is not in pool edges), so no conflict is detected. T1 is admitted.
6. T1 now occupies pool space permanently. It will never pass block template validation (dead input) and is not automatically evicted.
7. Repeat steps 1–6 with fresh cells to exhaust `max_tx_pool_size`, evicting legitimate transactions. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L993-1001)
```rust
fn check_rtx(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
) -> Result<TxStatus, Reject> {
    let short_id = rtx.transaction.proposal_short_id();
    let tx_status = get_tx_status(snapshot, &short_id);
    tx_pool.check_rtx_from_pool(rtx).map(|_| tx_status)
}
```

**File:** tx-pool/src/process.rs (L1016-1037)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
    let (succ, evicts) = match status {
        TxStatus::Fresh => tx_pool.add_pending(entry.clone())?,
        TxStatus::Gap => tx_pool.add_gap(entry.clone())?,
        TxStatus::Proposed => tx_pool.add_proposed(entry.clone())?,
    };
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
    }
    Ok(evicts)
}
```

**File:** tx-pool/src/process.rs (L1039-1056)
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

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** tx-pool/src/pool_cell.rs (L18-31)
```rust
impl<'a> CellProvider for PoolCell<'a> {
    fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
        if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
            return CellStatus::Dead;
        }
        if let Some((output, data)) = self.pool_map.get_output_with_data(out_point) {
            let cell_meta = CellMetaBuilder::from_cell_output(output, data)
                .out_point(out_point.to_owned())
                .build();
            CellStatus::live_cell(cell_meta)
        } else {
            CellStatus::Unknown
        }
    }
```

**File:** tx-pool/src/pool_cell.rs (L34-43)
```rust
impl<'a> CellChecker for PoolCell<'a> {
    fn is_live(&self, out_point: &OutPoint) -> Option<bool> {
        if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
            return Some(false);
        }
        if self.pool_map.get_output_with_data(out_point).is_some() {
            return Some(true);
        }
        None
    }
```

**File:** tx-pool/src/component/pool_map.rs (L23-28)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Status {
    Pending,
    Gap,
    Proposed,
}
```
