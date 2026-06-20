### Title
Stale IBD State Captured Before Snapshot Update Causes Incorrect `update_ibd_state` Notification - (File: `chain/src/verify.rs`)

---

### Summary

In `chain/src/verify.rs`, the IBD (Initial Block Download) state is sampled via `is_initial_block_download()` **before** the new chain snapshot is stored. Because `is_initial_block_download()` reads the current snapshot's tip timestamp to determine IBD status, it evaluates against the **old** tip. After the snapshot is updated with the new block's tip, the correct IBD state may differ. The stale value is then forwarded to the tx-pool and fee estimator via `update_ibd_state`, causing them to remain in IBD mode for one extra block at the exact IBD-exit transition.

---

### Finding Description

In `chain/src/verify.rs`, the function that processes a verified block executes the following sequence:

```
line 330: let in_ibd = self.shared.is_initial_block_download();
...
line 383: self.shared.store_snapshot(Arc::clone(&new_snapshot));   // tip updated
...
line 395: tx_pool_controller.update_ibd_state(in_ibd)             // stale value sent
``` [1](#0-0) [2](#0-1) [3](#0-2) 

`is_initial_block_download()` in `shared/src/shared.rs` determines IBD status by comparing the **current snapshot's** tip timestamp against `MAX_TIP_AGE`:

```rust
pub fn is_initial_block_download(&self) -> bool {
    if self.ibd_finished.load(Ordering::Acquire) {
        false
    } else if unix_time_as_millis().saturating_sub(self.snapshot().tip_header().timestamp())
        > MAX_TIP_AGE
    {
        true
    } else {
        self.ibd_finished.store(true, Ordering::Release);
        false
    }
}
``` [4](#0-3) 

At line 330, the snapshot still holds the **old** tip. The new block being committed may carry a recent timestamp that would cause `is_initial_block_download()` to return `false` (IBD finished). However, because the snapshot has not yet been updated, the function reads the old tip's timestamp, returns `true` (still in IBD), and the `ibd_finished` latch is **not** set. After `store_snapshot` at line 383 installs the new tip, the correct answer would be `false`, but `in_ibd` is already bound to the stale `true`. The tx-pool and fee estimator receive `update_ibd_state(true)` when they should receive `false`. [5](#0-4) 

---

### Impact Explanation

At the single block where the node transitions out of IBD (the first block whose timestamp is within `MAX_TIP_AGE` of wall-clock time), the tx-pool service and fee estimator are incorrectly notified that the node is **still in IBD**. Concretely:

- The fee estimator (`util/fee-estimator`) uses `update_ibd_state` to decide when to begin tracking fee data; it will skip fee tracking for this transition block.
- The tx-pool service uses the IBD flag to gate certain behaviors (e.g., transaction processing policy, block-assembler readiness).

The `ibd_finished` one-way latch means the error is self-correcting on the very next block, but the transition block is permanently missed by the fee estimator. For a node operator or miner relying on accurate fee estimation immediately after IBD, this produces a silent one-block gap in fee data at the most critical moment — the instant the node becomes "live."

---

### Likelihood Explanation

This affects **every** CKB node that performs a full sync from genesis. The IBD exit transition is a deterministic, unavoidable event. No special attacker action is required; any sync peer delivering the IBD-exit block triggers the condition. The likelihood is **certain** for all syncing nodes.

---

### Recommendation

Move the `in_ibd` capture to **after** `store_snapshot` so it reads the updated tip:

```rust
// BEFORE (incorrect order):
let in_ibd = self.shared.is_initial_block_download();  // line 330
...
self.shared.store_snapshot(Arc::clone(&new_snapshot)); // line 383
...
tx_pool_controller.update_ibd_state(in_ibd);           // line 395

// AFTER (correct order):
self.shared.store_snapshot(Arc::clone(&new_snapshot));
let in_ibd = self.shared.is_initial_block_download();  // now reads new tip
...
tx_pool_controller.update_ibd_state(in_ibd);
``` [6](#0-5) 

---

### Proof of Concept

1. Start a CKB node syncing from genesis (IBD mode).
2. The node receives blocks with old timestamps (all return `is_initial_block_download() == true`).
3. The node receives the first block `B_exit` whose timestamp satisfies `unix_time_as_millis() - B_exit.timestamp < MAX_TIP_AGE`.
4. At line 330, `is_initial_block_download()` reads the **previous** tip (old timestamp) → returns `true`; `ibd_finished` stays `false`.
5. At line 383, `store_snapshot` installs `B_exit` as the new tip.
6. At line 395, `update_ibd_state(true)` is sent — the tx-pool and fee estimator are told IBD is still active.
7. On the next block `B_exit+1`, line 330 now reads `B_exit`'s recent timestamp → returns `false`, sets `ibd_finished = true`, and `update_ibd_state(false)` is correctly sent.
8. The fee estimator has permanently missed `B_exit` as the first non-IBD block. [6](#0-5) [4](#0-3)

### Citations

**File:** chain/src/verify.rs (L330-397)
```rust
        let in_ibd = self.shared.is_initial_block_download();

        if new_best_block {
            info!(
                "[verify block] new best block found: {} => {:#x}, difficulty diff = {:#x}, unverified_tip: {}",
                block.header().number(),
                block.header().hash(),
                &cannon_total_difficulty - &current_total_difficulty,
                self.shared.get_unverified_tip().number(),
            );
            self.find_fork(&mut fork, current_tip_header.number(), block, ext);
            self.rollback(&fork, &db_txn)?;

            // update and verify chain root
            // MUST update index before reconcile_main_chain
            let begin_reconcile_main_chain = std::time::Instant::now();
            self.reconcile_main_chain(Arc::clone(&db_txn), &mut fork, switch)?;
            trace!(
                "reconcile_main_chain cost {:?}",
                begin_reconcile_main_chain.elapsed()
            );

            db_txn.insert_tip_header(&block.header())?;
            if new_epoch || fork.has_detached() {
                db_txn.insert_current_epoch_ext(&epoch)?;
            }
        } else {
            db_txn.insert_block_ext(&block.header().hash(), &ext)?;
        }
        db_txn.commit()?;

        if new_best_block {
            let tip_header = block.header();
            info!(
                "block: {}, hash: {:#x}, epoch: {:#}, total_diff: {:#x}, txs: {}, proposals: {}",
                tip_header.number(),
                tip_header.hash(),
                tip_header.epoch(),
                cannon_total_difficulty,
                block.transactions().len(),
                block.data().proposals().len()
            );

            self.update_proposal_table(&fork);
            let (detached_proposal_id, new_proposals) = self
                .proposal_table
                .finalize(origin_proposals, tip_header.number());
            fork.detached_proposal_id = detached_proposal_id;

            let new_snapshot =
                self.shared
                    .new_snapshot(tip_header, cannon_total_difficulty, epoch, new_proposals);

            self.shared.store_snapshot(Arc::clone(&new_snapshot));

            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
                if let Err(e) = tx_pool_controller.update_ibd_state(in_ibd) {
                    error!("Notify update_ibd_state error {}", e);
                }
```

**File:** shared/src/shared.rs (L382-393)
```rust
    pub fn is_initial_block_download(&self) -> bool {
        // Once this function has returned false, it must remain false.
        if self.ibd_finished.load(Ordering::Acquire) {
            false
        } else if unix_time_as_millis().saturating_sub(self.snapshot().tip_header().timestamp())
            > MAX_TIP_AGE
        {
            true
        } else {
            self.ibd_finished.store(true, Ordering::Release);
            false
        }
```
