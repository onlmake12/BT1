Audit Report

## Title
Per-removal `shrink_to_fit` on `MultiIndexVerifyEntryMap` causes O(N²) drain cost above ~700 queue entries — (`tx-pool/src/component/verify_queue.rs`)

## Summary
`VerifyQueue::remove_tx` unconditionally calls `self.shrink_to_fit()` on every successful removal. The `shrink_to_fit!` macro guard uses `SHRINK_THRESHOLD = 100`, which is structurally insufficient: after each shrink, hashbrown resets capacity to approximately `1.143 × len`, which immediately re-satisfies the guard condition on the very next pop for any queue above ~700 entries. This creates a self-reinforcing O(N) reallocation on every `pop_front` while the `verify_queue` write lock is held, degrading tx verification throughput to O(N²) total drain cost.

## Finding Description

**Root cause — `remove_tx` calls `shrink_to_fit` unconditionally:** [1](#0-0) 

Line 146 calls `self.shrink_to_fit()` inside the `.map()` closure, executing on every successful removal.

**The macro guard fires when `capacity > len + SHRINK_THRESHOLD`:** [2](#0-1) 

`SHRINK_THRESHOLD` is 100: [3](#0-2) 

**Why the guard fails — the self-reinforcing cycle:**

After `shrink_to_fit()` executes, hashbrown (the HashMap backend used via `rustc-hash` in `multi_index_map`) resets capacity to approximately `ceil(len / 0.875) ≈ 1.143 × len`. On the very next pop: `capacity = 1.143N`, `len = N − 1`. Guard check: `1.143N > (N − 1) + 100` → `0.143N > 99` → **N > 692**. For any queue above ~700 entries, every single pop triggers another O(N) shrink, which resets capacity to `1.143 × (N−1)`, which again satisfies the condition on the next pop.

**`pop_front` calls `remove_tx`, triggering the shrink on every normal verification:** [4](#0-3) 

**The verify worker holds the write lock for the entire duration of `pop_front`:** [5](#0-4) 

The write lock blocks all concurrent `add_tx` calls from new submissions during the O(N) shrink.

**The attacker's entry path — `submit_remote_tx` → `resumeble_process_tx` → `non_contextual_verify` → `enqueue_verify_queue`:** [6](#0-5) 

`non_contextual_verify` requires only structural validity — version, size, non-empty inputs/outputs, no duplicate deps, outputs data match, script hash type. No UTXO lookup, no script execution, no real CKB tokens: [7](#0-6) 

**The queue size limit is 256 MB:** [8](#0-7) 

At 500 bytes per transaction, this accommodates ~512,000 entries — far above the ~700-entry threshold.

## Impact Explanation

Once the queue exceeds ~700 entries, every call to `pop_front` triggers an O(N) `MultiIndexVerifyEntryMap` reallocation (rehashing all hash-indexed fields: `id`, `is_large_cycle`). The `verify_queue` write lock is held for the duration, blocking concurrent `add_tx` operations. The total cost to drain a queue of N entries becomes O(N²) instead of O(N). An attacker who fills the queue with structurally valid transactions (no real CKB tokens required) and sustains the submission rate can continuously degrade tx verification throughput for all peers sharing the node.

This maps to: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The attack requires only: (1) submitting transactions that pass `non_contextual_verify` (structural validity only — inputs may reference nonexistent UTXOs); (2) maintaining the queue above ~700 entries (well below the 256 MB cap); (3) sustaining the submission rate to keep the queue populated. Both the P2P relay path and the `send_transaction` RPC are unprivileged. The attacker spends O(N) bandwidth per unit time; the node pays O(N²) processing cost to drain the queue. The asymmetry is favorable to the attacker.

## Recommendation

Remove the `shrink_to_fit` call from `remove_tx`. Shrinking should be decoupled from the hot removal path entirely. Acceptable alternatives: (a) shrink only when the queue transitions from non-empty to empty (as `clear` already does); (b) shrink periodically every K removals; (c) raise the threshold above `len / 7` to account for hashbrown's post-shrink capacity surplus — but this is impractical as a static constant since it must scale with queue size. The correct fix is option (a) or (b).

## Proof of Concept

```
1. Connect to a CKB node via RPC or P2P.
2. Construct ~800 structurally valid transactions (pass non_contextual_verify;
   inputs may reference nonexistent UTXOs).
3. Submit all 800 transactions via send_transaction RPC or P2P relay.
4. Benchmark pop_front latency (verify worker processing rate) with queue at ~800 entries.
5. Observe: each pop triggers shrink_to_fit because
   capacity ≈ 1.143 × 800 = 914 > 800 + 100 = 900 ✓ (condition satisfied).
6. After each shrink, capacity resets to 1.143 × 799 ≈ 913 > 799 + 100 = 899 ✓ (still satisfied).
7. Compare against a patched build with shrink_to_fit removed from remove_tx.
8. Assert: unpatched per-pop latency is O(N); patched is O(1) amortized.
9. Sustain submission to keep queue full; measure sustained throughput degradation.

The invariant break is locally testable without mainnet access, PoW, or privileged roles.
```

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L18-18)
```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L19-19)
```rust
const SHRINK_THRESHOLD: usize = 100;
```

**File:** tx-pool/src/component/verify_queue.rs (L146-146)
```rust
            self.shrink_to_fit();
```

**File:** tx-pool/src/component/verify_queue.rs (L171-177)
```rust
    pub fn pop_front(&mut self, only_small_cycle: bool) -> Option<Entry> {
        if let Some(short_id) = self.peek(only_small_cycle) {
            self.remove_tx(&short_id)
        } else {
            None
        }
    }
```

**File:** util/src/shrink_to_fit.rs (L14-19)
```rust
macro_rules! shrink_to_fit {
    ($map:expr, $threshold:expr) => {{
        if $map.capacity() > ($map.len() + $threshold) {
            $map.shrink_to_fit();
        }
    }};
```

**File:** tx-pool/src/verify_mgr.rs (L130-145)
```rust
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
                    Some(entry) => entry,
                    None => {
                        if !tasks.is_empty() {
                            tasks.re_notify();
                            debug!(
                                "Worker (role: {:?}) didn't got tx after pop_front, but tasks is not empty, notify other Workers now",
                                self.role
                            );
                        }
                        return;
                    }
                }
            };
```

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

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```
