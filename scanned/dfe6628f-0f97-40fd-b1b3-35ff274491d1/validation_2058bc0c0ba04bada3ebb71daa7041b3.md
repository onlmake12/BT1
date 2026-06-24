Audit Report

## Title
VerifyQueue Hard-Cap Rejection With No Fee-Rate Eviction Enables Cheap Network-Wide DoS — (File: `tx-pool/src/component/verify_queue.rs`)

## Summary
`VerifyQueue` enforces a hard 256 MB total-size cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`) with no fee-rate-based eviction. Once an attacker fills the queue with ~512 minimum-fee transactions, every subsequent `add_tx` call returns `Reject::Full` regardless of the incoming transaction's fee rate, blocking both P2P relay (`submit_remote_tx`) and miner proposal (`notify_tx`) paths for all users. The main pool's `limit_size` eviction mechanism is never reached because transactions never exit the verify queue stage.

## Finding Description
`VerifyQueue.is_full` at [1](#0-0)  performs a pure size comparison with no fee-rate awareness. When it returns `true`, `add_tx` immediately rejects the incoming transaction at [2](#0-1)  with no attempt to evict a lower-fee-rate incumbent.

The `VerifyQueue` struct exposes only `remove_tx`, `remove_txs`, `remove_txs_by_peer`, `pop_front`, and `clear` — there is no `limit_size` or fee-rate-ordered eviction path. [3](#0-2) 

By contrast, the main pool's `limit_size` evicts the lowest-fee-rate entry in a loop until the pool fits within its configured limit. [4](#0-3) 

Both `submit_remote_tx` (P2P relay) and `notify_tx` (miner proposal) funnel through `resumeble_process_tx_and_notify_full_reject`, which calls `enqueue_verify_queue` → `add_tx`. [5](#0-4) 

There are no per-peer submission limits anywhere in the tx-pool code, so a single attacker can fill the entire 256 MB queue unimpeded.

## Impact Explanation
Once the verify queue is saturated, every `submit_remote_tx` and `notify_tx` call from every peer and every RPC caller returns `Reject::Full`. Legitimate high-fee transactions cannot displace attacker entries because there is no eviction path. The attacker can sustain the condition indefinitely by resubmitting as workers drain entries. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The entry path is fully unprivileged — any P2P peer or RPC caller can submit transactions. `TRANSACTION_SIZE_LIMIT` is 512 KB; filling 256 MB requires ~512 transactions. At the default minimum fee rate of 1 000 shannons/KB, each 512 KB transaction costs ~512 000 shannons (~0.005 CKB), making the total attack cost approximately 2–3 CKB. The attacker can declare cycles above `max_tx_verify_cycles` to route transactions to the slower large-cycle worker path, extending the denial window. The attack is repeatable and requires no special privileges or victim mistakes.

## Recommendation
**Short term**: In `add_tx`, when `is_full` returns `true`, compare the incoming transaction's fee rate against the lowest-fee-rate entry currently in the queue. If the incoming fee rate is higher, evict the lowest entry and admit the new one, mirroring the main pool's `limit_size` logic.

**Long term**: Introduce a `min_verify_queue_fee_rate` floor that rises dynamically as the queue fills, mirroring the main pool's dynamic minimum fee rate, so the verify queue cannot be cheaply saturated by minimum-fee spam.

## Proof of Concept
The existing unit test explicitly confirms the behavior: [6](#0-5) 

Manual reproduction steps:
1. Obtain ~3 CKB to fund ~512 transactions each consuming ~512 KB of serialized size.
2. Set each transaction's fee to exactly the `min_fee_rate` threshold (1 000 shannons/KB).
3. Optionally declare cycles as `max_tx_verify_cycles + 1` to route to the slow large-cycle worker.
4. Submit all transactions via `send_transaction` RPC or P2P relay.
5. After ~512 submissions, `verify_queue.total_tx_size` reaches 256 MB.
6. Every subsequent `send_transaction` from any user returns `PoolIsFull (-1106)`; every `notify_tx` from miners also returns `Reject::Full`.
7. Resubmit new transactions as workers drain old ones to sustain the DoS indefinitely.

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L128-177)
```rust
    /// Remove a tx from the queue
    pub fn remove_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
        self.inner.remove_by_id(id).map(|e| {
            let tx_size = e.inner.tx.data().serialized_size_in_block();
            if let Some(total_tx_size) = self.total_tx_size.checked_sub(tx_size) {
                self.total_tx_size = total_tx_size;
            } else if let Some(total_tx_size) = self.recompute_total_tx_size() {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, recomputed {}",
                    self.total_tx_size, tx_size, total_tx_size
                );
                self.total_tx_size = total_tx_size;
            } else {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, and recomputing overflowed",
                    self.total_tx_size, tx_size
                );
            }
            self.shrink_to_fit();
            e.inner
        })
    }

    /// Remove multiple txs from the queue
    pub fn remove_txs(&mut self, ids: impl Iterator<Item = ProposalShortId>) {
        for id in ids {
            self.remove_tx(&id);
        }
    }

    /// Remove multiple txs from the queue from a specified peer
    pub fn remove_txs_by_peer(&mut self, peer: &PeerIndex) {
        let ids: Vec<_> = self
            .inner
            .iter()
            .filter(|&(_cycle, entry)| entry.inner.remote.as_ref().is_some_and(|(_, p)| p == peer))
            .map(|(_cycle, entry)| entry.id.clone())
            .collect();

        self.remove_txs(ids.into_iter());
    }

    /// Returns the first entry in the queue and remove it
    pub fn pop_front(&mut self, only_small_cycle: bool) -> Option<Entry> {
        if let Some(short_id) = self.peek(only_small_cycle) {
            self.remove_tx(&short_id)
        } else {
            None
        }
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

**File:** tx-pool/src/pool.rs (L292-328)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
```

**File:** tx-pool/src/process.rs (L355-384)
```rust
    async fn resumeble_process_tx_and_notify_full_reject(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let tx_hash = tx.hash();
        let ret = self.resumeble_process_tx(tx, is_proposal_tx, remote).await;

        if matches!(ret, Err(Reject::Full(_))) {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }

        ret
    }

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
