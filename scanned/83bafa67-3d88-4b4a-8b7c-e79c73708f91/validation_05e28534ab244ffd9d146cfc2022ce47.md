Now I have all the information needed. Let me trace the exact code path.

### Title
Unconditional Orphan Retention on `Reject::Full` in High-Cycle Path Allows Orphan Pool Saturation — (`tx-pool/src/process.rs`)

---

### Summary

`process_orphan_tx` has two code paths for resolving orphans. The high-cycle path (orphan declared cycle > `max_tx_verify_cycles`) calls `enqueue_verify_queue` and, on **any** error, unconditionally retains the orphan. The low-cycle path correctly removes orphans on non-missing-input errors. When an attacker pre-fills the `verify_queue` to its 256 MB hard cap, `enqueue_verify_queue` returns `Reject::Full` for every high-cycle orphan whose parent just arrived. Those orphans are never removed, saturating the 100-slot orphan pool with unresolvable entries and evicting legitimate orphan transactions.

---

### Finding Description

**High-cycle path — unconditional retention on error:** [1](#0-0) 

```rust
if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
    match self.enqueue_verify_queue(orphan.tx.clone(), false, Some((orphan.cycle, orphan.peer))).await {
        Ok(_) => { self.remove_orphan_tx(&orphan_id).await; }
        Err(reject) => {
            warn!("process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}", ...);
            // ← NO remove_orphan_tx call; orphan is kept unconditionally
        }
    }
```

**Low-cycle path — correct eviction on non-missing-input errors:** [2](#0-1) 

```rust
if !is_missing_input(&reject) {
    self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
    ...
}
```

**`is_missing_input` only matches `Reject::Resolve(unknown)`:** [3](#0-2) 

```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

`Reject::Full` is not a `Reject::Resolve(...)`, so `is_missing_input` returns `false` for it — but the high-cycle path never calls `is_missing_input` at all.

**`enqueue_verify_queue` returns `Reject::Full` when the 256 MB cap is reached:** [4](#0-3) [5](#0-4) 

```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
...
if self.is_full(tx_size) {
    return Err(Reject::Full(format!("verify_queue total_tx_size exceeded, ...")));
}
```

**Orphan pool hard cap:** [6](#0-5) 

```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**Fee check is NOT performed before `enqueue_verify_queue`:** [7](#0-6) 

`resumeble_process_tx` calls `non_contextual_verify` (structure only) then immediately calls `enqueue_verify_queue`. The fee check lives inside `_process_tx → pre_check → check_tx_fee`, which runs only after dequeue. This means the attacker can fill the 256 MB queue with structurally valid, fee-invalid transactions at near-zero cost.

---

### Impact Explanation

Once the attacker's parent transaction is accepted and `process_orphan_tx` fires, every high-cycle orphan in the chain gets `Reject::Full` from `enqueue_verify_queue`. Because the high-cycle error branch has no eviction logic, all 100 orphan slots remain occupied by attacker-controlled entries whose parent is already confirmed but which can never advance to the verify_queue. Legitimate orphan transactions submitted by honest peers are evicted by `limit_size` (random eviction) when they try to enter the full pool. [8](#0-7) 

---

### Likelihood Explanation

The attack requires:
1. Submitting enough structurally-valid transactions to fill 256 MB of verify_queue — cheap because fee validation is deferred.
2. Pre-staging ≤100 orphan transactions with declared cycle > `max_tx_verify_cycles` — trivially crafted.
3. Submitting the parent while the queue is full.

All steps are reachable via standard P2P transaction relay by an unprivileged peer. No keys, hashpower, or privileged access are required.

**Mitigating factor:** Orphans expire after `ORPHAN_TX_EXPIRE_TIME = 100 × MAX_BLOCK_INTERVAL`, so the saturation is not permanent. The attacker must re-stage the attack periodically to sustain the effect.

---

### Recommendation

In the high-cycle error branch of `process_orphan_tx`, apply the same eviction logic used by the low-cycle path: remove the orphan unless the error is a genuine missing-input condition.

```rust
Err(reject) => {
    warn!("process_orphan {} failed to enqueue verify queue: {}", orphan.tx.hash(), reject);
    if !is_missing_input(&reject) {
        self.remove_orphan_tx(&orphan_id).await;
    }
}
```

This ensures that a transient `Reject::Full` does not permanently strand orphans whose parent has already been resolved.

---

### Proof of Concept

```
1. Connect to a CKB node as a peer.
2. Submit ~N structurally-valid transactions (pass non_contextual_verify, fail fee check)
   large enough to fill verify_queue to 256 MB.
3. Submit 100 orphan transactions O_1..O_100, each:
   - spending an output of a not-yet-submitted parent P
   - with declared_cycle > max_tx_verify_cycles
   These enter the orphan pool (DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100).
4. Submit parent P. P passes verification and is accepted.
   after_process calls process_orphan_tx(P).
5. For each O_i: enqueue_verify_queue returns Err(Reject::Full).
   The Err branch logs a warning and does NOT call remove_orphan_tx.
6. Assert: orphan pool still contains all 100 O_i entries.
7. Submit a legitimate orphan L: it is evicted immediately by limit_size
   because the pool is at capacity with attacker entries.
``` [9](#0-8) [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L598-624)
```rust
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

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L215-220)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
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
