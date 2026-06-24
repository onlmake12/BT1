Audit Report

## Title
Unconditional Orphan Retention on `Reject::Full` in High-Cycle Path Allows Orphan Pool Saturation — (`tx-pool/src/process.rs`)

## Summary
In `process_orphan_tx`, the high-cycle branch (orphan declared cycle > `max_tx_verify_cycles`) calls `enqueue_verify_queue` and, on any error, retains the orphan unconditionally. When the verify queue reaches its 256 MB hard cap, `enqueue_verify_queue` returns `Reject::Full`, causing all 100 orphan pool slots to remain occupied by attacker-controlled entries whose parent has already been resolved. Legitimate orphan transactions submitted by honest peers are then evicted by the random `limit_size` eviction logic.

## Finding Description
The high-cycle branch in `process_orphan_tx` ( [1](#0-0) ) calls `enqueue_verify_queue` and on `Err(reject)` only logs a warning — no `remove_orphan_tx` call is made. The orphan is unconditionally retained regardless of the rejection reason.

The low-cycle branch ( [2](#0-1) ) correctly applies `is_missing_input` and removes the orphan for any non-missing-input error.

`is_missing_input` only matches `Reject::Resolve(unknown)` ( [3](#0-2) ), so `Reject::Full` would not be treated as a missing-input condition — but the high-cycle path never calls it at all.

`enqueue_verify_queue` returns `Reject::Full` when the 256 MB cap is reached ( [4](#0-3) ) with the cap defined at ( [5](#0-4) ).

`resumeble_process_tx` only performs `non_contextual_verify` (structure check) before calling `enqueue_verify_queue` ( [6](#0-5) ), meaning fee validation is deferred until after dequeue. An attacker can fill the 256 MB queue with structurally valid, fee-invalid transactions at near-zero cost.

The orphan pool hard cap is 100 ( [7](#0-6) ), and overflow triggers random eviction ( [8](#0-7) ), evicting legitimate entries when the pool is full of attacker-controlled orphans.

## Impact Explanation
An unprivileged peer can saturate the 100-slot orphan pool with entries that can never advance to the verify queue, causing all legitimate orphan transactions to be randomly evicted. This degrades transaction relay across any targeted node and, if applied broadly, constitutes CKB network congestion achievable at low cost. This matches the **High (10001–15000 points)** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation
All attack steps are reachable via standard P2P transaction relay by an unprivileged peer. Filling 256 MB of verify queue requires submitting structurally valid transactions (fee check deferred), which is cheap. Pre-staging 100 orphans with declared cycle > `max_tx_verify_cycles` is trivial. The only timing requirement is that the parent transaction is submitted while the queue remains full. Orphans expire after `ORPHAN_TX_EXPIRE_TIME = 100 × MAX_BLOCK_INTERVAL` ( [9](#0-8) ), so the attacker must periodically re-stage the attack, but the per-iteration cost remains low.

## Recommendation
In the high-cycle error branch of `process_orphan_tx`, apply the same eviction logic used by the low-cycle path:

```rust
Err(reject) => {
    warn!(
        "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
        orphan.tx.hash(), reject, tx.hash(),
    );
    if !is_missing_input(&reject) {
        self.remove_orphan_tx(&orphan_id).await;
    }
}
```

This ensures a transient `Reject::Full` does not permanently strand orphans whose parent has already been resolved, while still retaining orphans that are genuinely waiting for a missing input.

## Proof of Concept
1. Connect to a CKB node as a peer.
2. Submit enough structurally valid (but fee-invalid) transactions to fill the verify queue to 256 MB (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`).
3. Submit 100 orphan transactions `O_1..O_100`, each spending an output of a not-yet-submitted parent `P`, with `declared_cycle > max_tx_verify_cycles`. All 100 enter the orphan pool.
4. Submit parent `P`. It passes verification and is accepted; `after_process` calls `process_orphan_tx(P)`.
5. For each `O_i`: `enqueue_verify_queue` returns `Err(Reject::Full)`. The error branch logs a warning and does **not** call `remove_orphan_tx`. All 100 orphan slots remain occupied.
6. Submit a legitimate orphan `L`: `limit_size` immediately evicts a random entry (likely `L` itself or another honest orphan) because the pool is at capacity with attacker entries.
7. Verify: orphan pool still contains all 100 `O_i` entries; `L` is not present.

### Citations

**File:** tx-pool/src/process.rs (L341-352)
```rust
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

**File:** tx-pool/src/component/verify_queue.rs (L215-219)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
```

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
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
