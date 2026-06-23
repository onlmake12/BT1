Based on my thorough reading of the relevant source files, here is my analysis:

---

### Title
Orphan Transaction with K Inputs from Same Parent Causes K-Fold Redundant Processing in `process_orphan_tx` — (`tx-pool/src/component/orphan.rs`)

### Summary

`find_by_previous` contains no deduplication logic. When a single orphan transaction has K inputs each spending a different output index of the same parent (`parent:0`, `parent:1`, ..., `parent:K-1`), the function returns the same `ProposalShortId` K times. `process_orphan_tx` then calls `_process_tx` K times for the same orphan in a single BFS iteration.

### Finding Description

**Root cause — `find_by_previous`:**

`add_orphan_tx` registers the orphan under each of its input `OutPoint`s as separate keys in `by_out_point`:

```rust
for out_point in tx.input_pts_iter() {
    self.by_out_point
        .entry(out_point)
        .or_default()
        .insert(tx.proposal_short_id());
}
``` [1](#0-0) 

An `OutPoint` is `(tx_hash, index)`. So `parent:0`, `parent:1`, `parent:2` are three distinct keys, each mapping to `{orphan_id}`.

`find_by_previous` then iterates over all output points of the resolved parent and flattens the results with **no deduplication**:

```rust
pub fn find_by_previous(&self, tx: &TransactionView) -> Vec<&ProposalShortId> {
    tx.output_pts()
        .iter()
        .filter_map(|out_point| self.by_out_point.get(out_point))
        .flatten()
        .collect::<Vec<_>>()
}
``` [2](#0-1) 

If the orphan has K inputs from the same parent (different indices), `find_by_previous` returns `[orphan_id, orphan_id, ..., orphan_id]` — K times.

**Propagation — `find_orphan_by_previous`:**

```rust
orphan.find_by_previous(tx)
    .iter()
    .filter_map(|id| orphan.get(id).cloned())
    .collect::<Vec<_>>()
``` [3](#0-2) 

The same `Entry` is cloned K times into the returned `Vec<OrphanEntry>`.

**Execution — `process_orphan_tx`:**

```rust
let orphans = self.find_orphan_by_previous(&previous).await;
for orphan in orphans.into_iter() {
    ...
    } else if let Some((ret, _snapshot)) = self
        ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
        .await
    {
``` [4](#0-3) 

`_process_tx` is called K times for the same orphan. The **first** call does full verification (including CKB-VM script execution via `verify_rtx`). Subsequent K-1 calls reach `pre_check`, which resolves inputs and acquires the `tx_pool` write lock before failing with `OutPointError::Dead` (inputs already spent). The write lock acquisition on each of the K-1 redundant calls blocks all concurrent tx-pool operations. [5](#0-4) 

### Impact Explanation

- **1 full CKB-VM verification** is performed (the first call).
- **K-1 redundant `pre_check` calls** each acquire the `tx_pool` write lock, blocking all other tx-pool operations (submission, block assembly, RBF checks) for the duration.
- With K inputs from the same parent, K can be large (bounded only by tx size limits; a tx with a ~512 KB body can carry thousands of inputs).
- The orphan pool holds up to 100 transactions (`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`). An attacker can pre-load 100 such crafted orphans, each with K inputs from a different parent. Resolving all parents triggers up to `100 × K` redundant `pre_check` + write-lock acquisitions in rapid succession. [6](#0-5) 

### Likelihood Explanation

The attack path is fully reachable by an unprivileged remote peer via P2P relay:

1. Attacker crafts a parent tx with K outputs and broadcasts it **after** the orphan.
2. Attacker crafts an orphan tx with K inputs (`parent:0` … `parent:K-1`) and relays it first — it lands in the orphan pool.
3. Attacker then relays the parent — `process_orphan_tx` fires and calls `_process_tx` K times for the same orphan.

The attacker pays fees proportional to tx size, but the victim node pays K× the processing cost. This is a concrete, locally testable amplification.

### Recommendation

Deduplicate the result of `find_by_previous` before returning, e.g.:

```rust
pub fn find_by_previous(&self, tx: &TransactionView) -> Vec<&ProposalShortId> {
    let mut seen = HashSet::new();
    tx.output_pts()
        .iter()
        .filter_map(|out_point| self.by_out_point.get(out_point))
        .flatten()
        .filter(|id| seen.insert(*id))
        .collect::<Vec<_>>()
}
```

Alternatively, deduplicate in `find_orphan_by_previous` or at the top of the `process_orphan_tx` loop.

### Proof of Concept

```rust
// Construct parent with 3 outputs
let parent = build_tx(vec![], 3);
// Construct orphan spending all 3 outputs of parent
let orphan = build_tx(
    vec![(&parent.hash(), 0), (&parent.hash(), 1), (&parent.hash(), 2)],
    1,
);
let mut pool = OrphanPool::new();
pool.add_orphan_tx(orphan.clone(), 0.into(), 0);

// find_by_previous returns 3 copies of the same short_id
let results = pool.find_by_previous(&parent);
assert_eq!(results.len(), 3);  // all three are the same orphan_id
assert_eq!(results[0], results[1]);
assert_eq!(results[1], results[2]);
// process_orphan_tx would call _process_tx 3 times for the same orphan
```

The existing test `test_orphan_duplicated` in `tx-pool/src/component/tests/orphan.rs` (line 65) already demonstrates that `find_by_previous(&tx1)` returns 3 results for 3 distinct orphans — the same mechanism produces 3 duplicates for a single orphan with 3 inputs from the same parent. [7](#0-6)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L150-155)
```rust
        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }
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

**File:** tx-pool/src/process.rs (L575-582)
```rust
    pub(crate) async fn find_orphan_by_previous(&self, tx: &TransactionView) -> Vec<OrphanEntry> {
        let orphan = self.orphan.read().await;
        orphan
            .find_by_previous(tx)
            .iter()
            .filter_map(|id| orphan.get(id).cloned())
            .collect::<Vec<_>>()
    }
```

**File:** tx-pool/src/process.rs (L595-627)
```rust
        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
            for orphan in orphans.into_iter() {
                if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
                    debug!(
                        "process_orphan {} added to verify queue; find previous from {}",
                        orphan.tx.hash(),
                        tx.hash(),
                    );
                    let orphan_id = orphan.tx.proposal_short_id();
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
                        }
                        Err(reject) => {
                            warn!(
                                "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );
                        }
                    }
                } else if let Some((ret, _snapshot)) = self
                    ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
                    .await
```

**File:** tx-pool/src/process.rs (L705-717)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/component/tests/orphan.rs (L65-69)
```rust
    let txs = orphan.find_by_previous(&tx1);
    assert_eq!(txs.len(), 3);
    assert!(txs.contains(&&tx2.proposal_short_id()));
    assert!(txs.contains(&&tx4.proposal_short_id()));
    assert!(txs.contains(&&tx5.proposal_short_id()));
```
