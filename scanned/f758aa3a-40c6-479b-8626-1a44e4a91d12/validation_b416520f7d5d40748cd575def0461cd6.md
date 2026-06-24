Audit Report

## Title
VerifyQueue Lacks Per-Peer Admission Limit, Enabling Large-Cycle Transaction Flood to DOS Tx-Pool Admission — (`tx-pool/src/component/verify_queue.rs`)

## Summary
The `VerifyQueue` enforces only a global 256 MB byte-size cap with no per-peer accounting. Any unprivileged P2P peer can flood the queue with structurally valid large-cycle transactions, saturating it and causing all subsequent calls to `submit_remote_tx`, `notify_tx`, and `process_orphan_tx` to return `Reject::Full`. This blocks remote relay, local RPC `send_transaction`, and orphan resolution for the duration of the attack.

## Finding Description
`VerifyQueue` tracks a single global counter `total_tx_size` against `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`. [1](#0-0) [2](#0-1) 

`add_tx` checks `is_full` before inserting and returns `Reject::Full` immediately with no per-peer quota: [3](#0-2) 

Transactions are classified as large-cycle based on the **peer-declared** cycle count, not actual verified cycles: [4](#0-3) 

Worker 0 is assigned `OnlySmallCycleTx` and skips all large-cycle entries via `pop_front(only_small_cycle=true)`: [5](#0-4) [6](#0-5) 

Critically, transactions enter the queue **before** any UTXO/contextual verification. `resumeble_process_tx` calls only `non_contextual_verify` (structural check) before calling `enqueue_verify_queue`: [7](#0-6) 

This means an attacker does not need valid UTXOs — only structurally valid transactions. All three admission paths funnel through `enqueue_verify_queue` → `add_tx`:

- `submit_remote_tx` (P2P relay): [8](#0-7) 
- `notify_tx` (local/RPC/proposal): [9](#0-8) 
- `process_orphan_tx` (large-cycle orphan promotion): [10](#0-9) 

No per-peer quota, rate limit, or eviction policy exists in the tx-pool layer. `remove_txs_by_peer` is only triggered on ban (malformed tx), which does not apply to structurally valid transactions.

The existing test explicitly confirms the `Reject::Full` behavior: [11](#0-10) 

## Impact Explanation
When the queue is saturated: remote transaction relay is blocked (node stops participating in mempool propagation), local RPC `send_transaction` fails with `PoolIsFull`, and large-cycle orphans that become resolvable remain stranded. This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer via the standard `RelayV3` protocol. The attacker needs only structurally valid transactions (no real UTXOs required, since UTXO resolution happens inside the worker after enqueuing). With `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes: [12](#0-11) 

filling the 256 MB queue requires approximately 512 transactions. A single peer can consume the entire queue budget. The attacker continuously submits replacement transactions as old ones drain, maintaining saturation indefinitely at very low cost.

## Recommendation
1. **Add per-peer byte quota in `add_tx`**: Track `total_tx_size_by_peer: HashMap<PeerIndex, usize>` in `VerifyQueue` and reject submissions from a peer exceeding `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / MAX_PEERS`.
2. **Evict large-cycle transactions from slow peers** when the queue approaches capacity, extending the existing `remove_txs_by_peer` mechanism.
3. **Apply a relay-layer rate limit per peer** before transactions reach `VerifyQueue`, analogous to the rate limiter in the hole-punching protocol.

## Proof of Concept
```
1. Attacker connects to target node as a P2P peer via RelayV3.
2. Attacker constructs ~512 structurally valid transactions (no valid UTXOs
   required), each ~512 KB serialized, declaring cycles = max_tx_verify_cycles + 1.
3. Attacker relays all 512 transactions via RelayTransactions messages.
4. Each transaction passes non_contextual_verify (structural only), enters
   VerifyQueue::add_tx(), is classified is_large_cycle = true.
   The OnlySmallCycleTx worker skips them; SubmitTimeFirst workers verify slowly.
5. total_tx_size approaches 256_000_000.
6. Any subsequent submit_remote_tx(), notify_tx(), or process_orphan_tx()
   (large-cycle path) returns Reject::Full.
7. Legitimate RPC send_transaction calls fail; orphan promotions are blocked.
8. Attacker continuously submits replacements as old ones drain, sustaining
   the saturated state indefinitely.
```

Reproducible via the existing test harness in `tx-pool/src/component/tests/chunk.rs` by extending `submit_remote_tx_notifies_relayer_when_verify_queue_is_full` to simulate concurrent peer submissions filling the queue from a single peer index.

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

**File:** tx-pool/src/component/verify_queue.rs (L187-191)
```rust
        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };
```

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
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

**File:** tx-pool/src/verify_mgr.rs (L185-189)
```rust
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
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

**File:** tx-pool/src/process.rs (L381-384)
```rust
    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
    }
```

**File:** tx-pool/src/process.rs (L604-624)
```rust
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

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```
