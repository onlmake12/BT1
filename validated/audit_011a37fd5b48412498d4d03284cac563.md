### Title
Verify Queue Unified Size Limit Allows Large-Cycle Tx Flood to Block All Tx Admission — (`tx-pool/src/component/verify_queue.rs`)

### Summary

The `VerifyQueue` enforces a single unified 256 MB size cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) with no per-class (large-cycle vs. small-cycle) quota. An unprivileged remote peer can flood the queue with large-cycle transactions that pass only the cheap `non_contextual_verify` structural check (no fee check, no script execution), fill the 256 MB cap with ~500 × 512 KB transactions, and cause every subsequent `add_tx` call — including legitimate small-cycle transactions — to return `Reject::Full`. The `OnlySmallCycleTx` worker, which is meant to protect small-cycle processing, becomes permanently idle because no small-cycle transactions can enter the full queue.

---

### Finding Description

**Entry point**: `TxPoolService::submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` → `VerifyQueue::add_tx`.

**Step 1 — Cheap admission gate.**
`resumeble_process_tx` calls only `non_contextual_verify` (structural consensus checks) before enqueuing. The fee check (`pre_check`) and script execution happen *after* dequeuing by a worker. An attacker therefore pays zero fee and executes zero VM cycles to get a transaction into the queue. [1](#0-0) 

**Step 2 — `is_large_cycle` set from declared cycles only.**
`VerifyQueue::add_tx` marks a transaction as large-cycle based solely on the *declared* cycle count supplied by the remote peer — no actual execution is required. [2](#0-1) 

**Step 3 — Single unified size cap, no per-class quota.**
`is_full` compares the incoming transaction's size against the single 256 MB global limit. There is no separate budget for large-cycle vs. small-cycle transactions. [3](#0-2) 

With `TRANSACTION_SIZE_LIMIT = 512 KB`, an attacker needs only ≈500 structurally valid transactions to exhaust the 256 MB cap. [4](#0-3) 

**Step 4 — `OnlySmallCycleTx` worker becomes permanently idle.**
Worker 0 (when `worker_num > 1`) is assigned `WorkerRole::OnlySmallCycleTx` and calls `pop_front(true)`, which returns `None` when the queue contains only large-cycle entries. It then calls `re_notify` and returns — it never processes anything. [5](#0-4) [6](#0-5) 

**Step 5 — All new tx admissions rejected.**
Once `total_tx_size ≥ 256 MB`, every call to `add_tx` — regardless of cycle class — returns `Err(Reject::Full(...))`. Legitimate small-cycle transactions cannot enter the queue at all. [7](#0-6) 

---

### Impact Explanation

- All new transaction submissions (P2P relay and RPC) are rejected with `Reject::Full` for the duration of the attack.
- The `OnlySmallCycleTx` worker — the intended protection for small-cycle transactions — is rendered useless because the protection only applies *inside* the queue, not at admission.
- `SubmitTimeFirst` workers slowly drain the attacker's large-cycle transactions (each requires full script verification before being rejected for bad fee/inputs), keeping the queue full for an extended window.
- The attack is self-sustaining: as workers drain entries, the attacker can re-submit new ones to keep the queue full.

---

### Likelihood Explanation

- **Cost**: Craft ≈500 structurally valid transactions (~512 KB each, declared cycles > `max_tx_verify_cycles`). No fee payment, no PoW, no script execution required at admission time.
- **Access**: Any peer with a P2P connection can relay transactions via the standard relay protocol.
- **No existing per-peer rate limit** in the verify queue admission path; `remove_txs_by_peer` is only triggered after a peer is banned for sending a *malformed* transaction, which these transactions are not.
- **Default worker count** is `max(num_cpus * 3/4, 1)`, so on a typical server with ≥2 cores, worker 0 is `OnlySmallCycleTx` and is idle throughout the attack. [8](#0-7) 

---

### Recommendation

1. **Separate size quotas**: Reserve a fixed fraction of `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` exclusively for small-cycle transactions (e.g., 50 MB out of 256 MB). `is_full` should check the appropriate sub-quota based on `is_large_cycle`.
2. **Per-peer admission limit**: Track per-`PeerIndex` bytes currently in the verify queue and reject new entries from a peer that already holds more than a configurable threshold.
3. **Fee pre-check before enqueue**: Perform at least a lightweight fee-rate check (using the declared cycles and tx size) before admitting a transaction to the queue, so zero-fee flood transactions are rejected at the gate.

---

### Proof of Concept

```
1. Connect to a CKB node via P2P.
2. Craft N = ceil(256_000_000 / 512_000) ≈ 500 transactions:
   - Each ~512 KB serialized size.
   - Declared cycles > max_tx_verify_cycles (e.g., max_tx_verify_cycles + 1).
   - Structurally valid (passes non_contextual_verify): valid cell structure,
     valid capacity, valid script hashes — but inputs can be non-existent
     (fee/resolve check happens only after dequeue).
3. Relay all 500 transactions via RelayTransactions P2P message.
4. Assert: verify_queue.total_tx_size ≈ 256 MB.
5. Submit a legitimate small-cycle transaction (declared cycles < max_tx_verify_cycles).
6. Observe: Reject::Full("verify_queue total_tx_size exceeded...").
7. Measure: OnlySmallCycleTx worker processes 0 transactions during the flood window.
```

The existing test `submit_remote_tx_notifies_relayer_when_verify_queue_is_full` in `tx-pool/src/component/tests/chunk.rs` already demonstrates the `Reject::Full` path when `total_tx_size` is near the limit — confirming the mechanism is reachable in production. [9](#0-8)

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

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
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

**File:** tx-pool/src/component/verify_queue.rs (L211-214)
```rust
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
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

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** tx-pool/src/verify_mgr.rs (L180-203)
```rust
        let workers: Vec<_> = (0..worker_num)
            .map({
                let tasks = Arc::clone(&service.verify_queue);
                let signal_exit = signal_exit.clone();
                move |idx| {
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
                    };
                    let (child_tx, child_rx) = watch::channel(ChunkCommand::Resume);
                    (
                        child_tx,
                        Worker::new(
                            service.clone(),
                            Arc::clone(&tasks),
                            child_rx,
                            signal_exit.clone(),
                            role,
                        ),
                    )
                }
            })
            .collect();
```

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```

**File:** tx-pool/src/component/tests/chunk.rs (L376-402)
```rust
#[tokio::test]
async fn submit_remote_tx_notifies_relayer_when_verify_queue_is_full() {
    let (service, tx_relay_receiver) = service_with_relay_receiver();
    let tx = build_tx(vec![(&H256([1; 32]).into(), 0)], 1);
    let tx_hash = tx.hash();

    service
        .verify_queue
        .write()
        .await
        .set_total_tx_size_for_test(256_000_000 - 1);

    let ret = service
        .submit_remote_tx(tx, MAX_TX_VERIFY_CYCLES, 1.into())
        .await;

    assert!(matches!(ret, Err(crate::error::Reject::Full(_))));
    match tx_relay_receiver
        .try_recv()
        .expect("expected reject notification")
    {
        TxVerificationResult::Reject { tx_hash: rejected } => {
            assert_eq!(rejected, tx_hash);
        }
        _ => panic!("expected reject notification"),
    }
}
```
