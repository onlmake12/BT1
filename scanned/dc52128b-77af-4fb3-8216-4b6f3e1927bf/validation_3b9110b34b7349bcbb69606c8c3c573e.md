Audit Report

## Title
Out-of-bounds output index on pool parent causes `CellStatus::Unknown` instead of `Dead`, enabling free orphan-pool flooding — (`tx-pool/src/pool_cell.rs`)

## Summary
`PoolCell::cell` returns `CellStatus::Unknown` when a parent transaction is present in the pool but the referenced output index is out of bounds, because `get_output_with_data` cannot distinguish "tx not in pool" from "tx in pool, index OOB" — both return `None`. The `Unknown` status propagates through `OverlayCellProvider` → `resolve_transaction` → `OutPointError::Unknown` → `is_missing_input() == true`, causing the child transaction to be silently accepted into the orphan pool with no fee check, enabling an attacker to permanently occupy all 100 orphan slots at negligible cost.

## Finding Description
`PoolMap::get_output_with_data` uses `and_then` chaining: it first looks up the parent by `ProposalShortId`, then calls `output_with_data(index)`. If the parent is in the pool but `index >= N`, `output_with_data` returns `None` (out-of-bounds), and the whole function returns `None` — indistinguishable from "tx not in pool at all." [1](#0-0) 

`PoolCell::cell` maps this `None` to `CellStatus::Unknown`: [2](#0-1) 

`OverlayCellProvider::cell` delegates `Unknown` to the snapshot layer, which also returns `Unknown` (the parent is not yet on-chain): [3](#0-2) 

`resolve_transaction` maps `Unknown` to `OutPointError::Unknown`: [4](#0-3) 

`is_missing_input` returns `true` for `OutPointError::Unknown`: [5](#0-4) 

`after_process` calls `add_orphan` when `is_missing_input` is `true`, bypassing all fee checks: [6](#0-5) 

When the parent is eventually committed, `process_orphan_tx` re-processes the orphan. The snapshot returns `Unknown` for the OOB index (the output simply doesn't exist on-chain), so `is_missing_input` is `true` again and the orphan is **not** removed — it stays until TTL expiry: [7](#0-6) 

The orphan pool has a hard cap of 100 slots with random eviction of legitimate entries when full: [8](#0-7) [9](#0-8) 

## Impact Explanation
An unprivileged P2P peer can permanently occupy all 100 orphan pool slots across all reachable nodes at negligible cost (no fees required for the crafted child transactions). This prevents legitimate orphan transactions from being retained and processed, disrupting transaction relay for any user whose transaction depends on an unconfirmed parent. The attack is sustainable indefinitely and can be applied to the entire network simultaneously, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attacker only needs to submit one valid transaction `P` with `N` outputs into the pool (trivially achievable, even with minimum fee), then craft up to 100 child transactions referencing `(P.hash, index >= N)` with varying witnesses to produce distinct tx hashes. No fees are required for the children. The attack is repeatable as slots expire (TTL = `100 * MAX_BLOCK_INTERVAL`), and can be executed by any P2P peer without any special privilege.

## Recommendation
In `PoolCell::cell`, distinguish the two `None` cases from `get_output_with_data` by first checking whether the parent tx is present in the pool:

```rust
fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
    if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
        return CellStatus::Dead;
    }
    let parent_in_pool = self.pool_map
        .get(&ProposalShortId::from_tx_hash(&out_point.tx_hash()))
        .is_some();
    if parent_in_pool {
        match self.pool_map.get_output_with_data(out_point) {
            Some((output, data)) => {
                let cell_meta = CellMetaBuilder::from_cell_output(output, data)
                    .out_point(out_point.to_owned())
                    .build();
                CellStatus::live_cell(cell_meta)
            }
            None => CellStatus::Dead, // index OOB on a known tx → treat as Dead
        }
    } else {
        CellStatus::Unknown // parent genuinely not seen yet
    }
}
```

This ensures `resolve_transaction` returns `OutPointError::Dead` for OOB-indexed inputs, which `is_missing_input` correctly treats as non-orphan-eligible, causing the transaction to be rejected rather than orphaned.

## Proof of Concept
1. Submit a parent tx `P` with exactly 1 output (index 0) into the pool via RPC.
2. Via P2P relay, submit a child tx whose single input references `(P.hash, index=1)`.
3. Observe `tx_pool_info.orphan == 1`; the child is in the orphan pool.
4. Repeat with 100 distinct child txs (vary witness bytes to produce distinct tx hashes) to fill all 100 orphan slots.
5. Submit a legitimate orphan (valid parent not yet in pool); observe it is immediately evicted.
6. After `P` is committed on-chain, re-run step 2: the invalid orphan is re-added (not cleared), confirming the self-clearing failure.

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

**File:** tx-pool/src/pool_cell.rs (L23-30)
```rust
        if let Some((output, data)) = self.pool_map.get_output_with_data(out_point) {
            let cell_meta = CellMetaBuilder::from_cell_output(output, data)
                .out_point(out_point.to_owned())
                .build();
            CellStatus::live_cell(cell_meta)
        } else {
            CellStatus::Unknown
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
