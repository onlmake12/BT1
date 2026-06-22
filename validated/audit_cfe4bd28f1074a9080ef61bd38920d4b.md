The code path is fully traceable. Let me walk through each step.

**Step 1 — `get_output_with_data` conflates two distinct `None` cases** [1](#0-0) 

`get_output_with_data` first looks up the entry by `tx_hash`, then calls `output_with_data(index)`. If the parent tx **is** in the pool but `index >= N`, `output_with_data` returns `None` (out-of-bounds), and the whole function returns `None` — indistinguishable from "tx not in pool at all." [2](#0-1) 

**Step 2 — `PoolCell::cell` maps that `None` to `CellStatus::Unknown`** [3](#0-2) 

When `get_output_with_data` returns `None` (whether because the tx is absent or because the index is OOB), `PoolCell::cell` returns `CellStatus::Unknown`.

**Step 3 — `OverlayCellProvider` falls through to the snapshot** [4](#0-3) 

`Unknown` from the pool layer causes the overlay to delegate to the snapshot. The snapshot also returns `Unknown` (the parent tx is not on-chain yet).

**Step 4 — `resolve_transaction` maps `Unknown` to `OutPointError::Unknown`** [5](#0-4) 

**Step 5 — `is_missing_input` returns `true` for `OutPointError::Unknown`** [6](#0-5) 

**Step 6 — `after_process` adds the tx to the orphan pool** [7](#0-6) 

For a remote (P2P-relayed) transaction, `is_missing_input` returning `true` triggers `add_orphan`. No fee check is ever reached because `pre_check` returns early on the `Unknown` error before `check_tx_fee` is called. [8](#0-7) 

**Step 7 — Orphan pool has a hard cap of 100, evicting legitimate entries** [9](#0-8) [10](#0-9) 

When the orphan pool is full, random legitimate orphans are evicted.

**Step 8 — The orphan never self-clears**

When the parent tx is eventually committed, `process_orphan_tx` re-processes the orphan. The snapshot still returns `Unknown` for the OOB index (the output simply doesn't exist on-chain either), so `is_missing_input` is `true` again, and the orphan **stays** in the pool until it expires (`100 * MAX_BLOCK_INTERVAL`). [11](#0-10) 

---

### Title
Out-of-bounds output index on pool parent causes `CellStatus::Unknown` instead of `Dead`, enabling free orphan-pool flooding — (`tx-pool/src/pool_cell.rs`)

### Summary
`PoolCell::cell` returns `CellStatus::Unknown` when a parent transaction is present in the pool but the referenced output index is out of bounds. This is semantically wrong: the cell is not "unknown" (the parent is known), it is invalid. The `Unknown` status propagates through `OverlayCellProvider` → `resolve_transaction` → `OutPointError::Unknown` → `is_missing_input() == true`, causing the child transaction to be silently accepted into the orphan pool at zero cost (no fee check is reached).

### Finding Description
`PoolMap::get_output_with_data` returns `Option<(CellOutput, Bytes)>` and cannot distinguish between two cases:
- The parent tx hash is not in the pool at all.
- The parent tx hash **is** in the pool, but `out_point.index()` exceeds the number of outputs.

Both cases return `None`, and `PoolCell::cell` maps both to `CellStatus::Unknown`. The correct behavior for the second case is `CellStatus::Dead` (or a new `Invalid` variant), which would cause `resolve_transaction` to return `OutPointError::Dead`, which `is_missing_input` correctly treats as non-orphan-eligible.

### Impact Explanation
An unprivileged P2P peer can:
1. Submit (or observe) any valid transaction `P` with `N` outputs entering the pool.
2. Craft child transactions referencing `(P.hash, index >= N)` with varying witnesses/outputs to produce distinct tx hashes.
3. Submit them via P2P relay. Each is accepted into the 100-slot orphan pool at zero fee cost.
4. Continuously refill the orphan pool as slots expire, permanently evicting legitimate orphans.
5. After `P` is committed, the invalid orphans remain until their TTL expires (`100 * MAX_BLOCK_INTERVAL`), wasting memory and re-processing work.

### Likelihood Explanation
The attack requires only a valid parent tx in the pool (trivially achievable by the attacker themselves) and the ability to send P2P relay messages. No fees are required. The orphan pool cap of 100 limits the instantaneous damage but the attacker can sustain the attack indefinitely at negligible cost.

### Recommendation
In `PoolCell::cell`, distinguish the two `None` cases from `get_output_with_data`:

```rust
fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
    if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
        return CellStatus::Dead;
    }
    // Check if the parent tx is in the pool at all
    let parent_in_pool = self.pool_map
        .get(&ProposalShortId::from_tx_hash(&out_point.tx_hash()))
        .is_some();
    if parent_in_pool {
        // Parent is known; if output_with_data returns None, the index is OOB → Dead
        match self.pool_map.get_output_with_data(out_point) {
            Some((output, data)) => { /* build CellMeta, return Live */ }
            None => CellStatus::Dead,  // index out of bounds on a known tx
        }
    } else {
        CellStatus::Unknown  // parent genuinely not seen yet
    }
}
```

### Proof of Concept
1. Insert a parent tx with exactly 1 output (index 0) into the pool.
2. Via P2P relay, submit a child tx whose single input references `(parent_tx_hash, index=1)`.
3. Observe the child is added to the orphan pool (`tx_pool_info.orphan == 1`).
4. The rejection reason is never surfaced as `Dead`/`Invalid`; the node believes the parent is simply "not yet seen."
5. Repeat with 100 distinct child txs to fill the orphan pool and evict legitimate orphans.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L183-190)
```rust
    pub(crate) fn get_output_with_data(&self, out_point: &OutPoint) -> Option<(CellOutput, Bytes)> {
        self.get(&ProposalShortId::from_tx_hash(&out_point.tx_hash()))
            .and_then(|entry| {
                entry
                    .transaction()
                    .output_with_data(out_point.index().into())
            })
    }
```

**File:** util/types/src/core/views.rs (L346-357)
```rust
    pub fn output_with_data(&self, idx: usize) -> Option<(packed::CellOutput, Bytes)> {
        self.data().raw().outputs().get(idx).map(|output| {
            let data = self
                .data()
                .raw()
                .outputs_data()
                .get(idx)
                .should_be_ok()
                .raw_data();
            (output, data)
        })
    }
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

**File:** util/types/src/core/cell.rs (L461-467)
```rust
    fn cell(&self, out_point: &OutPoint, eager_load: bool) -> CellStatus {
        match self.overlay.cell(out_point, eager_load) {
            CellStatus::Live(cell_meta) => CellStatus::Live(cell_meta),
            CellStatus::Dead => CellStatus::Dead,
            CellStatus::Unknown => self.cell_provider.cell(out_point, eager_load),
        }
    }
```

**File:** util/types/src/core/cell.rs (L706-714)
```rust
                    let cell_status = cell_provider.cell(out_point, eager_load);
                    match cell_status {
                        CellStatus::Dead => Err(OutPointError::Dead(out_point.clone())),
                        CellStatus::Unknown => Err(OutPointError::Unknown(out_point.clone())),
                        CellStatus::Live(cell_meta) => {
                            entry.insert(cell_meta.clone());
                            Ok(cell_meta)
                        }
                    }
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

**File:** tx-pool/src/process.rs (L286-312)
```rust
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
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/process.rs (L651-665)
```rust
                            if !is_missing_input(&reject) {
                                self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                                if reject.is_malformed_tx() {
                                    self.ban_malformed(orphan.peer, format!("reject {reject}"))
                                        .await;
                                }
                                if reject.is_allowed_relay() {
                                    self.send_result_to_relayer(TxVerificationResult::Reject {
                                        tx_hash: orphan.tx.hash(),
                                    });
                                }
                                if reject.should_recorded() {
                                    self.put_recent_reject(&orphan.tx.hash(), &reject).await;
                                }
                            }
```

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```
