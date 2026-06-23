I need to trace the exact code path carefully before rendering a verdict.

### Title
RBF Race Condition Admits Tx With Unresolvable Input Into Pool — (`tx-pool/src/process.rs`, `tx-pool/src/pool.rs`)

---

### Summary

A TOCTOU race between `pre_check` (read lock) and `submit_entry` (write lock) allows a transaction whose conflicting pool-parent was concurrently RBF'd away to be admitted with an input cell that no longer exists in the pool or on-chain. The stale `rtx` is stored in the pool entry, and the block-template builder selects it without re-validating inputs, producing a block that the network rejects.

---

### Finding Description

The tx-pool processes a transaction in three sequential phases, each with a different lock scope:

**Phase 1 — `pre_check` (read lock)**

When tx B's input cell C is already spent by pool tx A, the normal resolve fails with `OutPointError::Dead`. The code falls into the RBF branch:

```
resolve_tx(tx_pool, &snapshot, tx.clone(), true)   // rbf=true
```

`PoolCell` with `rbf=true` skips the dead-input check and returns cell C as live (from tx A's output). `find_conflict_outpoint` confirms tx A is the conflict. `pre_check` returns `Ok(...)` and **releases the read lock**. [1](#0-0) [2](#0-1) 

**Race window — `verify_rtx` (no lock held)**

Script verification runs without any pool lock. During this window a second submitter successfully RBFs tx A with tx A' (different outputs, same or different inputs). Tx A is removed from the pool; cell C is evicted from `edges.inputs`. [3](#0-2) 

**Phase 2 — `submit_entry` (write lock)**

```rust
let conflicts = if tx_pool.enable_rbf() {
    tx_pool.check_rbf(&snapshot, &entry)?   // ← re-checks conflicts
} else { ... };
```

`check_rbf` calls `find_conflict_tx`, which scans B's inputs against `edges.inputs`. Because tx A was already removed, cell C is no longer registered there. `conflict_ids.is_empty()` → `check_rbf` returns `Ok(HashSet::new())`. [4](#0-3) [5](#0-4) 

The only other guard that could catch this is `check_rtx`, but it is **only invoked when the chain tip changed**:

```rust
if pre_resolve_tip != tip_hash {
    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;
    ...
}
```

A concurrent RBF does not advance the tip. `pre_resolve_tip == tip_hash`, so `check_rtx` is **skipped entirely**. [6](#0-5) 

`process_rbf` is a no-op (empty conflict set). `_submit_entry` → `add_pending` → `pool_map.add_entry` → `record_entry_edges` inserts cell C into `edges.inputs` for tx B. The insert succeeds because cell C was already evicted when tx A was removed. Tx B is admitted with a stale `rtx` whose `resolved_inputs` reference cell data from tx A's output — a cell that no longer exists anywhere. [7](#0-6) [8](#0-7) 

**Block template**

`package_txs` → `TxSelector::txs_to_commit` selects transactions by fee rate without re-resolving inputs. Tx B can be included in a block template. A miner that mines this template produces a block the network rejects (cell C is unknown), constituting a consensus deviation. [9](#0-8) 

---

### Impact Explanation

A tx with an unresolvable input is admitted to the pool and can appear in a block template. If mined, the block fails network-level validation (cell C is neither in the UTXO set nor in any ancestor block), causing the miner to waste PoW and the node to diverge from consensus. The node's view of the mempool is also corrupted: tx B occupies a slot, its stale `rtx` holds phantom cell data, and any child tx of B would also be unresolvable.

---

### Likelihood Explanation

The race window is the entire duration of `verify_rtx` (script execution), which can span millions of cycles — easily hundreds of milliseconds. An attacker controlling two connections can reliably hit this window by:

1. Submitting tx A (spending cell C).
2. Submitting tx B (spending cell C, higher fee — enters RBF path, starts slow script verification).
3. Immediately submitting tx A' (spending cell C, even higher fee — RBFs tx A, completes quickly while B is still verifying).

No privileged access, leaked keys, or majority hashpower is required. Both submissions are standard P2P/RPC transaction relay paths.

---

### Recommendation

In `submit_entry`, unconditionally re-validate the `rtx` against the current pool state — not only when the tip changes. Specifically, after acquiring the write lock and before calling `_submit_entry`, always call `check_rtx_from_pool` (or an equivalent that uses `PoolCell(rbf=false)`) to confirm every resolved input is still live. If any input is `Unknown` or `Dead`, reject the entry.

```rust
// Always re-check, not only on tip change
let status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;
``` [10](#0-9) [11](#0-10) 

---

### Proof of Concept

```
Thread 1 (attacker, tx B):
  pre_check(B) → read lock
    resolve_tx(rbf=false) → Dead(C)
    resolve_tx(rbf=true)  → Ok (C treated as live from A)
    find_conflict_outpoint → Some(A)
    return Ok(tip_T, rtx_B_with_A_data, ...)
  read lock released
  [verify_rtx running — no lock held]

Thread 2 (attacker, tx A'):
  pre_check(A') → read lock → Ok
  read lock released
  verify_rtx(A') → Ok
  submit_entry(A') → write lock
    check_rbf → finds A as conflict → removes A from pool
    _submit_entry(A') → admitted
  write lock released

Thread 1 (attacker, tx B) resumes:
  submit_entry(B) → write lock
    check_rbf → find_conflict_tx(B) → edges.inputs has no entry for C → empty
    pre_resolve_tip == tip_hash → check_rtx SKIPPED
    process_rbf(empty) → no-op
    _submit_entry(B) → add_pending → record_entry_edges(C→B) → admitted
  write lock released

Result: B in pool, rtx_B.resolved_inputs[0] = stale cell meta from A's output
        C does not exist in pool or chain
        block template may include B → mined block rejected by network
```

### Citations

**File:** tx-pool/src/process.rs (L103-116)
```rust
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };
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

**File:** tx-pool/src/process.rs (L136-137)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
```

**File:** tx-pool/src/process.rs (L292-309)
```rust
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
```

**File:** tx-pool/src/process.rs (L724-732)
```rust
        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```

**File:** tx-pool/src/pool_cell.rs (L19-31)
```rust
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

**File:** tx-pool/src/pool.rs (L536-554)
```rust
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

**File:** tx-pool/src/pool.rs (L581-585)
```rust
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }
```

**File:** tx-pool/src/component/pool_map.rs (L294-298)
```rust
    pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
        tx.input_pts_iter()
            .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
            .collect()
    }
```

**File:** tx-pool/src/component/edges.rs (L33-54)
```rust
    pub(crate) fn insert_input(
        &mut self,
        out_point: OutPoint,
        txid: ProposalShortId,
    ) -> Result<(), Reject> {
        // inputs is occupied means double speanding happened here
        match self.inputs.entry(out_point.clone()) {
            Entry::Occupied(occupied) => {
                let msg = format!(
                    "txpool unexpected double-spending out_point: {:?} old_tx: {:?} new_tx: {:?}",
                    out_point,
                    occupied.get(),
                    txid
                );
                Err(Reject::RBFRejected(msg))
            }
            Entry::Vacant(vacant) => {
                vacant.insert(txid);
                Ok(())
            }
        }
    }
```
