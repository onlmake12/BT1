### Title
Unbounded Synchronous CKB-VM Execution in `process_orphan_tx` Bypasses Chunk-Command Pause Mechanism — (`tx-pool/src/process.rs`)

---

### Summary

When a transaction is successfully verified from the `VerifyQueue`, `Worker::process_inner` calls `after_process`, which calls `process_orphan_tx`. Inside `process_orphan_tx`, every resolved orphan child is verified by calling `_process_tx` with `command_rx = None`, bypassing the chunk-command pause/interrupt mechanism. An unprivileged peer can pre-fill the orphan pool with up to `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` max-cycle transactions forming a dependency chain, then submit the root parent. This forces the verify worker to execute up to 100 full CKB-VM verifications synchronously and without interruption, blocking the worker and delaying all other transactions in the verify queue.

---

### Finding Description

**Step 1 — Normal verify-queue processing (pauseable):**

`Worker::process_inner` pops one entry from the `VerifyQueue` and calls `_process_tx` with a live `command_rx`, allowing the CKB-VM execution to be paused or aborted by the chunk-command mechanism. [1](#0-0) 

**Step 2 — `after_process` triggers orphan resolution:**

On a successful result, `after_process` unconditionally calls `process_orphan_tx` for the verified transaction. [2](#0-1) [3](#0-2) 

**Step 3 — Orphan children verified without pause mechanism:**

`process_orphan_tx` runs a BFS over the orphan pool. For each orphan whose declared cycle count is ≤ `max_tx_verify_cycles`, it calls `_process_tx` with `command_rx = None`. This means the CKB-VM execution for every orphan child **cannot be paused or interrupted**. [4](#0-3) 

The critical call site: [5](#0-4) 

Compare with the pauseable call in the worker: [6](#0-5) 

**Step 4 — Orphan pool size bound:**

The orphan pool is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. [7](#0-6) 

An attacker can pre-fill all 100 slots with transactions forming a linear chain (each spending the output of the previous), each declaring the maximum allowed cycles. When the root parent is submitted and verified, `process_orphan_tx` resolves the entire chain synchronously, executing up to 100 full CKB-VM verifications without any pause point.

---

### Impact Explanation

The verify worker (`Worker::process_inner`) is blocked for the entire duration of all orphan verifications — up to `100 × max_tx_verify_cycles` cycles — before it can return to the `VerifyQueue` to process the next entry. All legitimate transactions waiting in the verify queue are delayed for the full duration. With multiple workers (`max_tx_verify_workers`), an attacker can target all workers simultaneously by submitting multiple parent transactions in quick succession, each resolving a pre-staged orphan chain.

---

### Likelihood Explanation

Any unprivileged peer reachable via the relay protocol can call `submit_remote_tx` to inject transactions into the orphan pool. The orphan pool accepts transactions whose inputs are not yet known. Filling 100 slots requires submitting 100 transactions with unknown parents, which is trivially achievable. The attacker must time the parent submission before orphan expiry (`ORPHAN_TX_EXPIRE_TIME = 100 × MAX_BLOCK_INTERVAL`), which is a wide window. [8](#0-7) [9](#0-8) 

---

### Recommendation

Pass the worker's `command_rx` into `_process_tx` calls made from `process_orphan_tx`, or impose a per-call limit on the number of orphans resolved synchronously (e.g., move high-count orphan resolution to the `VerifyQueue` instead of processing inline). This mirrors the fix recommended in the VUSD report: add a guard equivalent to `nonReentrant` — in CKB's case, ensure the chunk-command pause mechanism is always active during CKB-VM execution triggered by queue processing.

---

### Proof of Concept

1. Attacker connects to a CKB node as a relay peer.
2. Attacker constructs a chain of 100 transactions: `T0 → T1 → T2 → … → T99`, where each `Ti` spends an output of `T(i-1)`, and each declares `max_tx_verify_cycles` cycles.
3. Attacker relays `T1` through `T99` (all orphans, since `T0` is not yet known). The orphan pool fills to capacity.
4. Attacker relays `T0` (the root parent). The node verifies `T0` via the `VerifyQueue` (pauseable).
5. On success, `after_process` → `process_orphan_tx` resolves `T1`, then `T2`, …, then `T99` — each via `_process_tx(…, None)` — executing 99 additional full CKB-VM verifications without any pause point.
6. The verify worker is blocked for the entire duration. All other transactions in the `VerifyQueue` are stalled. [10](#0-9) [11](#0-10)

### Citations

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

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

**File:** tx-pool/src/process.rs (L496-501)
```rust
                    self.send_result_to_relayer(TxVerificationResult::Ok {
                        original_peer: Some(peer),
                        tx_hash,
                    });
                    self.process_orphan_tx(&tx).await;
                }
```

**File:** tx-pool/src/process.rs (L530-537)
```rust
                    Ok(_) => {
                        debug!("after_process local send_result_to_relayer {}", tx_hash);
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
                        self.process_orphan_tx(&tx).await;
                    }
```

**File:** tx-pool/src/process.rs (L591-671)
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
                        }
                        Err(reject) => {
                            debug!(
                                "process_orphan {} reject {}, find previous from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );

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
                        }
                    }
                }
            }
        }
    }
```

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
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
