### Title
Unbounded Orphan Transaction Queue Processing in `process_orphan_tx` Causes Verify Worker Starvation — (`tx-pool/src/process.rs`)

### Summary

`process_orphan_tx` in `tx-pool/src/process.rs` performs an unbounded BFS traversal over the orphan pool with no per-call iteration limit. An unprivileged remote peer can pre-fill the orphan pool with a chain of up to 100 transactions, then submit the root transaction to trigger sequential full script verification of all 100 orphans in a single verify-worker invocation, stalling the tx-pool service.

### Finding Description

`process_orphan_tx` is called after every successfully verified transaction (from `after_process`). It uses an unbounded `while let Some(previous) = orphan_queue.pop_front()` BFS loop: [1](#0-0) 

For each orphan whose inputs are now satisfied, it calls `_process_tx` with `command_rx: None` (the synchronous/non-pausable path): [2](#0-1) 

On success, the resolved orphan is pushed back into `orphan_queue`, cascading through the entire chain: [3](#0-2) 

There is no limit on how many orphans are processed per invocation. The orphan pool is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`: [4](#0-3) 

But the BFS loop in `process_orphan_tx` has no corresponding per-call bound. All 100 entries can be drained in a single call, each requiring a full `verify_rtx` execution (CKB-VM script verification) with `command_rx: None`, meaning no pause/suspend mechanism is active: [5](#0-4) 

The verify worker's `process_inner` loop calls `after_process` (which calls `process_orphan_tx`) synchronously after each tx verification, before picking the next entry from the verify queue: [6](#0-5) 

### Impact Explanation

The verify worker is occupied for the entire duration of the unbounded orphan chain traversal. During this time, no other transactions in the `VerifyQueue` are processed. With 100 orphans each declared at up to `max_tx_verify_cycles` (70,000,000 cycles by default per `ckb.toml`): [7](#0-6) 

...the worker can be stalled for a prolonged period. This delays or denies service to all other pending transactions in the verify queue, constituting a tx-pool DoS. The `VerifyQueue` has a 256 MB size cap but no time-based or count-based processing limit per worker iteration: [8](#0-7) 

### Likelihood Explanation

The attack requires only an unprivileged P2P peer. The attacker:
1. Sends 100 orphan transactions forming a linear chain (each spending the output of the previous) — the orphan pool accepts up to 100 entries before evicting: [9](#0-8) 
2. Sends the root transaction (the one that resolves the first orphan's input). This is a standard `send_transaction` relay message.
3. When the root tx is accepted, `after_process` triggers `process_orphan_tx`, which cascades through all 100 orphans.

No special privilege, key, or majority hashpower is required. The attack is repeatable and low-cost.

### Recommendation

Introduce a per-call iteration limit parameter to `process_orphan_tx`, analogous to the recommendation in the reference report. For example, process at most `N` orphans per invocation (e.g., `N = 10`) and re-schedule remaining work asynchronously. This mirrors the fix applied to `_updateWithdrawalQueue`: pass a parameter defining how many requests to process per call, preventing a single invocation from monopolizing the verify worker.

### Proof of Concept

1. Connect to a CKB node as a P2P peer.
2. Construct a chain of 100 transactions: `tx[0]` spends a live cell; `tx[i]` spends output 0 of `tx[i-1]` for `i = 1..99`. Each `tx[i]` for `i >= 1` is an orphan (its parent is not yet in the pool).
3. Submit `tx[1]` through `tx[99]` via `send_transaction` RPC or P2P relay. Each is added to the orphan pool via `add_orphan_tx`. The pool fills to 100 entries: [10](#0-9) 
4. Submit `tx[0]` (the root). It passes pre-check and enters the verify queue normally.
5. The verify worker picks up `tx[0]`, verifies it, calls `after_process` → `process_orphan_tx(tx[0])`.
6. `process_orphan_tx` enters the BFS loop: finds `tx[1]`, calls `_process_tx(tx[1], Some(cycle), None)`, succeeds, pushes `tx[1]` into `orphan_queue`; finds `tx[2]`, calls `_process_tx(tx[2], ...)`, and so on through all 100 orphans — all within a single invocation, with no iteration cap: [11](#0-10) 
7. The verify worker is blocked for the entire duration. All other transactions in the `VerifyQueue` are delayed until the loop completes.

### Citations

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

**File:** tx-pool/src/verify_mgr.rs (L147-158)
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
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```
