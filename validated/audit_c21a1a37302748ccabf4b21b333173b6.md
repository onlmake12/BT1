### Title
Orphan Transaction Verified Multiple Times Concurrently Due to Missing Pre-Removal Before Async Script Execution — (`tx-pool/src/process.rs`)

---

### Summary

`TxPoolService::process_orphan_tx` reads orphan transactions from the orphan pool and then calls the long-running async `_process_tx` (which performs full CKB-VM script verification) **without first removing the orphan from the pool**. Because all locks are released during the async verification, concurrent invocations of `process_orphan_tx` — triggered by different parent transactions being accepted simultaneously by multiple verify workers — can each independently discover the same orphan and execute full script verification on it redundantly. This is a TOCTOU (time-of-check-time-of-use) state-ordering defect directly analogous to the Olympus `activeProposal` reset-after-loop pattern: the guarding state is read, an expensive external operation is performed, and the state is only updated after the operation completes.

---

### Finding Description

In `tx-pool/src/process.rs`, `process_orphan_tx` (lines 591–671) implements a BFS over the orphan pool to promote orphans whose parents have just been accepted:

```
591: pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
592:     let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
593:     orphan_queue.push_back(tx.clone());
595:     while let Some(previous) = orphan_queue.pop_front() {
596:         let orphans = self.find_orphan_by_previous(&previous).await;  // read lock acquired + released
597:         for orphan in orphans.into_iter() {
598:             if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
...
625:             } else if let Some((ret, _snapshot)) = self
626:                 ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)  // ← LONG ASYNC, no lock held
627:                 .await
628:             {
629:                 match ret {
630:                     Ok(_) => {
...
640:                         self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;  // ← removed ONLY after success
641:                         orphan_queue.push_back(orphan.tx);
```

The orphan is **not removed from the orphan pool before `_process_tx` is called**. `_process_tx` calls `pre_check` (read lock, released immediately), then `verify_rtx` (no lock — full CKB-VM script execution), then `submit_entry` (write lock). The entire verification window is lock-free.

`process_orphan_tx` is called from `after_process` (lines 500, 536) which is invoked by each verify worker in `verify_mgr.rs` (line 157) after successfully accepting a transaction. Multiple workers run concurrently.

**Concurrent exploitation path:**

1. Attacker submits orphan `O` with two inputs: `(parent_A_hash, 0)` and `(parent_B_hash, 0)`, with expensive scripts (cycles ≤ `max_tx_verify_cycles` to use the inline path).
2. Attacker submits `parent_A` and `parent_B` concurrently (two simultaneous RPC `send_transaction` calls or two P2P relay messages).
3. Worker 1 accepts `parent_A` → calls `process_orphan_tx(parent_A)` → `find_orphan_by_previous` returns `O` (still in orphan pool) → calls `_process_tx(O)` (releases all locks, begins script execution).
4. Worker 2 accepts `parent_B` → calls `process_orphan_tx(parent_B)` → `find_orphan_by_previous` also returns `O` (still in orphan pool, not yet removed) → calls `_process_tx(O)` concurrently.
5. Both workers execute full CKB-VM script verification of `O` simultaneously.
6. The first to reach `submit_entry`'s write lock succeeds; the second fails at `check_txid_collision` inside `pre_check` — but only after completing full verification.

The same race applies within a single `process_orphan_tx` call if `find_orphan_by_previous` returns the same orphan ID multiple times (when the orphan references multiple outputs of the same parent, since `by_out_point` maps each input out-point separately). [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

Each redundant `_process_tx` call executes full CKB-VM script verification up to `max_tx_verify_cycles` (70,000,000 cycles by default) before failing. An attacker who submits `N` orphans each with inputs from two different parents, then submits all `2N` parents concurrently, causes `2N` concurrent verifications of `N` transactions — doubling the CPU cost of orphan promotion. With expensive scripts (e.g., secp256k1 signature verification), this is a sustained CPU exhaustion vector reachable by any unprivileged P2P peer or RPC caller. The node's transaction processing throughput degrades proportionally. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

The race is reachable by any unprivileged peer or RPC caller. No special privileges, keys, or majority hashpower are required. The attacker only needs to:
- Submit orphan transactions (valid P2P relay or RPC `send_transaction`)
- Submit parent transactions concurrently (two simultaneous RPC calls or two P2P relay messages to different peers)

The concurrent worker architecture (`VerifyMgr` spawns multiple workers) makes this race structurally likely under normal load, not just under adversarial conditions. [6](#0-5) [7](#0-6) 

---

### Recommendation

Remove the orphan from the orphan pool **before** calling `_process_tx`, mirroring the checks-effects-interactions pattern. Specifically, in the inline path (line 625), call `self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await` before `_process_tx`. If `_process_tx` subsequently fails, the orphan can be re-inserted (or simply dropped, since the parent is now accepted and the orphan's failure is definitive). This is the direct analog of the Olympus fix: reset the active state before the external call loop, not after.

Alternatively, mark the orphan as "in-flight" (e.g., via a `DashSet` of in-progress orphan IDs) before calling `_process_tx` and clear the mark afterward, so concurrent `find_orphan_by_previous` calls skip already-being-processed orphans. [8](#0-7) [9](#0-8) 

---

### Proof of Concept

```
1. Attacker crafts orphan_O with:
   - input_0: (parent_A_hash, 0)
   - input_1: (parent_B_hash, 0)
   - lock script: expensive computation, cycles ≤ max_tx_verify_cycles

2. Attacker relays orphan_O to the node via P2P (RelayV3).
   → Node: orphan_O added to OrphanPool (both out-points registered in by_out_point).

3. Attacker concurrently submits parent_A and parent_B via two simultaneous
   RPC send_transaction calls (or two P2P relay messages).

4. VerifyMgr Worker-1 accepts parent_A:
   → after_process(parent_A) → process_orphan_tx(parent_A)
   → find_orphan_by_previous(parent_A) returns [orphan_O]  ← orphan_O still in pool
   → _process_tx(orphan_O) begins CKB-VM execution (releases all locks)

5. VerifyMgr Worker-2 accepts parent_B (concurrently with step 4):
   → after_process(parent_B) → process_orphan_tx(parent_B)
   → find_orphan_by_previous(parent_B) returns [orphan_O]  ← orphan_O STILL in pool
   → _process_tx(orphan_O) begins CKB-VM execution (releases all locks)

6. Both workers execute full script verification of orphan_O simultaneously.
   CPU cost: 2 × (actual verification cycles of orphan_O).

7. Worker-1 reaches submit_entry write lock first → orphan_O accepted into pool.
   Worker-2 reaches pre_check → check_txid_collision fails → returns early.
   But Worker-2 already completed full script verification before this point.

Amplification: repeat with N orphans × 2 parents each → 2N redundant verifications.
```

### Citations

**File:** tx-pool/src/process.rs (L496-501)
```rust
                    self.send_result_to_relayer(TxVerificationResult::Ok {
                        original_peer: Some(peer),
                        tx_hash,
                    });
                    self.process_orphan_tx(&tx).await;
                }
```

**File:** tx-pool/src/process.rs (L584-586)
```rust
    pub(crate) async fn remove_orphan_tx(&self, id: &ProposalShortId) {
        self.orphan.write().await.remove_orphan_tx(id);
    }
```

**File:** tx-pool/src/process.rs (L591-641)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

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
                {
                    match ret {
                        Ok(_) => {
                            self.send_result_to_relayer(TxVerificationResult::Ok {
                                original_peer: Some(orphan.peer),
                                tx_hash: orphan.tx.hash(),
                            });
                            debug!(
                                "process_orphan {} success, find previous from {}",
                                orphan.tx.hash(),
                                tx.hash()
                            );
                            self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                            orphan_queue.push_back(orphan.tx);
```

**File:** tx-pool/src/process.rs (L705-732)
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

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

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

**File:** tx-pool/src/verify_mgr.rs (L86-103)
```rust
    async fn run(mut self) {
        let queue_ready = self.tasks.read().await.subscribe();
        self.refresh_status();
        loop {
            tokio::select! {
                _ = self.exit_signal.cancelled() => {
                    break;
                }
                _ = self.command_rx.changed() => {
                    self.status = self.command_rx.borrow_and_update().to_owned();
                    self.process_inner().await;
                }
                _ = queue_ready.notified() => {
                    self.process_inner().await;
                }
            };
        }
    }
```

**File:** tx-pool/src/verify_mgr.rs (L147-162)
```rust
            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
```
