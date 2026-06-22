### Title
Verify Queue Can Be Flooded with Structurally Valid but Semantically Invalid Transactions, Delaying Legitimate Transaction Processing - (File: `tx-pool/src/component/verify_queue.rs`, `tx-pool/src/process.rs`)

---

### Summary

CKB's tx-pool uses a `VerifyQueue` to stage incoming transactions before full verification. Transactions are admitted to this queue after only a cheap structural (`non_contextual_verify`) check. Fee validity and input existence are checked later, inside the worker, after the transaction is already occupying queue space. An unprivileged remote peer can flood the queue with many structurally valid but semantically invalid transactions (e.g., referencing non-existent inputs), filling the 256 MB queue and causing legitimate transactions to be rejected or delayed.

---

### Finding Description

The `VerifyQueue` in `tx-pool/src/component/verify_queue.rs` is a FIFO priority queue sorted by `added_time`. Transactions enter via `resumeble_process_tx` in `tx-pool/src/process.rs`:

```
resumeble_process_tx
  → non_contextual_verify   ← only structural check (no fee, no input existence)
  → enqueue_verify_queue    ← tx is now in the queue, occupying space
``` [1](#0-0) 

The fee check (`check_tx_fee`) and input resolution (`resolve_tx`) happen inside `_process_tx` → `pre_check`, which is called by the background worker **after** the transaction has already been dequeued: [2](#0-1) 

The queue enforces a single admission gate: total serialized size must not exceed `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` bytes: [3](#0-2) [4](#0-3) 

When the queue is full, new transactions are rejected with `Reject::Full`: [5](#0-4) 

Workers dequeue entries in `added_time` order (FIFO for same-cycle class) via `pop_front`: [6](#0-5) [7](#0-6) 

There is no per-peer rate limit, no minimum fee enforced at queue admission, and no mechanism to skip or evict attacker entries already in the queue.

---

### Impact Explanation

An attacker who fills the 256 MB verify queue with minimum-size (~100–200 byte) structurally valid transactions can:

1. **Reject legitimate transactions**: Any transaction submitted while the queue is full receives `Reject::Full` and is dropped.
2. **Delay in-queue transactions**: The `SubmitTimeFirst` worker processes entries strictly in `added_time` order. Legitimate transactions submitted after the attacker's batch must wait for all attacker entries to be dequeued and rejected by the worker before their own verification begins.
3. **Waste node CPU**: Each attacker transaction triggers a full `pre_check` cycle (snapshot read, cell resolution attempt) before being rejected.

Transactions referencing non-existent inputs pass `non_contextual_verify`, enter the queue, and are only rejected inside the worker after consuming queue space and CPU. They are then routed to the orphan pool (via `is_missing_input`), which has its own separate size limit, compounding the resource pressure. [8](#0-7) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged remote peer via the standard transaction relay protocol (`submit_remote_tx`). No privileged access, no key material, no majority hashpower required.
- **Cost**: Crafting structurally valid transactions with fake inputs requires no on-chain funds. The attacker only pays network bandwidth.
- **Repeatability**: After the worker drains the queue, the attacker can immediately re-flood it. There is no cooldown or per-peer admission throttling at the verify queue level.
- **Peer banning**: `ban_malformed` is only triggered for `is_malformed_tx()` rejections (structural failures). Transactions rejected for missing inputs (`is_missing_input`) do not trigger a ban, so the attacker peer is not disconnected. [9](#0-8) 

---

### Recommendation

1. **Enforce a minimum fee rate at queue admission**: Before calling `enqueue_verify_queue`, perform a lightweight fee-rate plausibility check using the declared output capacity minus input capacity (for locally-known inputs) or a flat minimum-byte-fee floor. This mirrors the recommendation in the reference report to add a minimum deposit.
2. **Per-peer queue slot limit**: Track how many bytes each remote peer has in the verify queue and reject new submissions from that peer once their share exceeds a threshold.
3. **Expose priority eviction**: Allow the queue to evict the lowest-fee-rate entry when full and a higher-fee-rate entry arrives, analogous to the reference report's recommendation to expose a function that releases a specific queue index.

---

### Proof of Concept

**Attacker steps:**

1. Connect to a CKB node as a peer.
2. Craft ~1.28 million transactions (each ~200 bytes serialized), each spending a randomly generated, non-existent `OutPoint`. These pass `non_contextual_verify` because that check is purely structural.
3. Relay all transactions via the standard relay protocol. Each call reaches `resumeble_process_tx` → `non_contextual_verify` (passes) → `enqueue_verify_queue` (admitted).
4. The verify queue reaches its 256 MB limit.
5. Alice submits a legitimate transaction via RPC or relay. She receives `Reject::Full`.
6. Workers begin draining the attacker's transactions. Each is rejected at `pre_check` for `OutPointError::Unknown` and routed to the orphan pool. The orphan pool fills, evicting legitimate orphans.
7. Alice's transaction remains unprocessable until the entire attacker batch is drained.

**Key code path confirming no fee/input check at admission:** [1](#0-0) [10](#0-9)

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

**File:** tx-pool/src/process.rs (L500-513)
```rust
                    self.process_orphan_tx(&tx).await;
                }
                Err(reject) => {
                    debug!(
                        "after_process {} {} remote reject: {} ",
                        tx_hash, peer, reject
                    );
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
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

**File:** tx-pool/src/component/verify_queue.rs (L180-194)
```rust
    pub fn peek(&self, only_small_cycle: bool) -> Option<ProposalShortId> {
        let mut iter = self.inner.iter_by_added_time();

        if let Some(proposal_entry) = iter.find(|e| e.is_proposal_tx) {
            return Some(proposal_entry.inner.tx.proposal_short_id());
        }

        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };

        entry.map(|e| e.inner.tx.proposal_short_id())
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L196-237)
```rust
    /// If the queue did not have this tx present, true is returned.
    /// If the queue did have this tx present, false is returned.
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
    }
```
