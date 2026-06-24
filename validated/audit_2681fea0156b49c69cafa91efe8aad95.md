Audit Report

## Title
Verify Queue Size Limit Exhaustion Enables Sustained Transaction Submission DoS - (File: `tx-pool/src/component/verify_queue.rs`)

## Summary

`VerifyQueue::add_tx` enforces a hard 256 MB byte-size ceiling as the sole admission gate before script execution. Because `resumeble_process_tx` performs only structural (`non_contextual_verify`) validation before calling `enqueue_verify_queue`, an unprivileged attacker can flood the queue with structurally valid but contextually invalid transactions at zero on-chain cost, causing every subsequent legitimate `send_transaction` call to return `Reject::Full` for the duration of the attack.

## Finding Description

**Root cause — `verify_queue.rs` lines 17–19 and 103–106:**

`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` is the sole admission gate. [1](#0-0) 

`is_full` checks only the running byte total with no per-sender quota: [2](#0-1) 

`add_tx` returns `Err(Reject::Full(...))` the moment `total_tx_size` reaches 256 MB: [3](#0-2) 

**Admission path — `process.rs` lines 335–353:**

`resumeble_process_tx` calls only `non_contextual_verify` (structure only), then immediately calls `enqueue_verify_queue` with no fee-rate or cell-existence check: [4](#0-3) 

Both the RPC path (`notify_tx` → `resumeble_process_tx_and_notify_full_reject`) and the P2P relay path (`submit_remote_tx` → same wrapper) converge here: [5](#0-4) 

Fee-rate validation (`check_tx_fee`) and cell resolution (`resolve_tx`) are deferred to `_process_tx`, which runs only **after** a worker dequeues the entry: [6](#0-5) 

The queue slot is therefore occupied for the full worker-processing latency even for zero-fee or cell-missing transactions.

**Existing guards and why they fail:**

- *Duplicate check* (`verify_queue_contains`): Keyed by `ProposalShortId` derived from tx hash. An attacker using a fresh random `OutPoint` per submission produces a unique hash each time, bypassing this check entirely. [7](#0-6) 

- *P2P rate limiter* (`sync/src/relayer/mod.rs` lines 89–92): 30 `RelayTransactions` messages/s per peer, keyed by `(PeerIndex, message_type)`. This limits message count, not transaction count or byte volume per message. Multiple Sybil peers multiply throughput linearly. [8](#0-7) 

- *RPC path*: No per-IP or per-connection rate limit is applied before `resumeble_process_tx`. The RPC `send_transaction` path has no admission throttle at all.

With `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes, the queue is exhausted by approximately 500 max-size transactions: [9](#0-8) 

Workers drain slots quickly on DB-miss (non-existent `OutPoint`), but the attacker re-submits immediately, sustaining the full state indefinitely.

## Impact Explanation

Once `total_tx_size` reaches 256 MB, every call to `add_tx` from any source returns `Err(Reject::Full(...))`. The RPC layer surfaces this as `PoolIsFull (-1106)`; the P2P relay layer marks the transaction as rejected. Legitimate fee-paying users are indistinguishable from the attacker at the admission gate and are equally rejected. This constitutes **sustained, low-cost CKB network congestion** matching the allowed **High-severity** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The attack requires no privileged access, no mining power, and no on-chain funds. Crafting structurally valid transactions referencing non-existent `OutPoint`s is trivial. Via the RPC path there is no rate limit at all. Via P2P, a single peer can fill the queue in under 17 seconds at 30 messages/second with one max-size transaction per message; multiple Sybil peers reduce this further. The attack is fully repeatable and self-sustaining as long as the attacker matches the worker drain rate.

## Recommendation

1. **Short term**: Enforce a minimum fee-rate check (or non-zero fee check) inside `resumeble_process_tx` **before** calling `enqueue_verify_queue`, so below-threshold transactions are rejected at the gate without consuming queue space.
2. **Short term**: Add a per-peer (P2P session or RPC connection) byte quota on in-flight verify-queue entries, analogous to `remove_txs_by_peer` which already exists for cleanup but is not used proactively for admission control. [10](#0-9) 
3. **Long term**: Implement RPC-level per-IP rate limiting for `send_transaction` to bound the submission rate from any single source.

## Proof of Concept

```python
# Attacker loop (pseudocode):
for i in range(500):
    tx = build_tx(
        inputs=[OutPoint(tx_hash=random_nonexistent_hash(), index=0)],
        outputs=[Output(capacity=100_CKB)],
        witnesses=[b'\x00' * 512_000],  # pad to TRANSACTION_SIZE_LIMIT
    )
    rpc.send_transaction(tx)
    # passes non_contextual_verify (verify_queue.rs:342), enters VerifyQueue

# After ~500 submissions, total_tx_size ≈ 256 MB.
# Legitimate user:
result = rpc.send_transaction(valid_tx)
# → PoolIsFull (-1106)

# Attacker re-submits as workers drain slots (fast DB-miss on non-existent OutPoints):
# sustains the full state indefinitely.
```

The exact gate is `verify_queue.rs:104–106` (`is_full`) and `verify_queue.rs:215–220` (`add_tx` rejection). The unguarded admission path is `process.rs:335–353` (`resumeble_process_tx`) and `process.rs:860–868` (`enqueue_verify_queue`).

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L108-111)
```rust
    /// Returns true if the queue contains a tx with the specified id.
    pub fn contains_key(&self, id: &ProposalShortId) -> bool {
        self.inner.get_by_id(id).is_some()
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L159-168)
```rust
    pub fn remove_txs_by_peer(&mut self, peer: &PeerIndex) {
        let ids: Vec<_> = self
            .inner
            .iter()
            .filter(|&(_cycle, entry)| entry.inner.remote.as_ref().is_some_and(|(_, p)| p == peer))
            .map(|(_cycle, entry)| entry.id.clone())
            .collect();

        self.remove_txs(ids.into_iter());
    }
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

**File:** tx-pool/src/process.rs (L371-384)
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

    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
    }
```

**File:** tx-pool/src/process.rs (L860-868)
```rust
    async fn enqueue_verify_queue(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let mut queue = self.verify_queue.write().await;
        queue.add_tx(tx, is_proposal_tx, remote)
    }
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```
