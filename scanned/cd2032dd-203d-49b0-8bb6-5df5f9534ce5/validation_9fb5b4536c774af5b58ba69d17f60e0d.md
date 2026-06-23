### Title
Global `VerifyQueue` Can Be Filled by Any Unprivileged P2P Peer to Block All Transaction Submissions — (File: tx-pool/src/component/verify_queue.rs)

---

### Summary

The `VerifyQueue` in CKB's tx-pool is a globally shared, size-bounded queue that holds transactions pending contextual verification. It has a hard-coded 256 MB ceiling with **no per-peer quota**. Any unprivileged P2P peer can flood this queue with transactions that pass non-contextual (structural) checks but fail contextual checks, filling the queue and causing every subsequent transaction submission — from every user — to be rejected with `Reject::Full`. This is a direct analog to the global withdrawal-queue griefing described in the external report.

---

### Finding Description

`VerifyQueue` (`tx-pool/src/component/verify_queue.rs`) is the staging area between initial structural validation and full script/contextual verification. Its global ceiling is the hard-coded constant:

```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
``` [1](#0-0) 

The fullness check is purely global — it compares the running `total_tx_size` counter against this constant with no per-peer accounting:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [2](#0-1) 

When `is_full` returns `true`, `add_tx` immediately returns `Reject::Full` for **every** caller, regardless of origin:

```rust
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
``` [3](#0-2) 

The entry path for a remote peer is `submit_remote_tx` → `resumeble_process_tx_and_notify_full_reject` → `resumeble_process_tx`. Non-contextual verification (structural checks only — no input-existence check, no fee check) is performed first; if it passes, the transaction is enqueued. Contextual verification (resolve + script execution) happens later, inside the worker pool: [4](#0-3) 

Workers are spawned by `VerifyMgr`. One worker is dedicated to small-cycle transactions; the rest process in submission-time order. A single peer submitting many large-cycle transactions can starve all workers and keep the queue full: [5](#0-4) 

The `remove_txs_by_peer` method exists to purge a peer's entries on disconnect, but it provides no protection while the peer remains connected: [6](#0-5) 

The maximum per-transaction size is `TRANSACTION_SIZE_LIMIT = 512 KB`: [7](#0-6) 

Filling the 256 MB queue therefore requires approximately 512 maximum-size transactions — a trivially small number for a sustained P2P attack.

The `Reject::Full` error propagates directly to the RPC layer as `PoolIsFull (-1106)`, so both P2P-relayed and RPC-submitted transactions are blocked: [8](#0-7) 

---

### Impact Explanation

While the verify queue is saturated, **every** call to `submit_remote_tx` or `send_transaction` (RPC) returns `Reject::Full`. No new transaction from any user can enter the pool. The attacker can sustain this state indefinitely by continuously submitting fresh transactions as workers drain old ones. This degrades the node to a state where it cannot relay or mine any new user transactions, mirroring the "all withdrawals queued" impact in the external report.

---

### Likelihood Explanation

- **Entry path**: Any peer that completes a P2P handshake can relay transactions via the standard `RelayTransactions` message — no privilege required.
- **Cost**: Non-contextual verification does not check input existence or fee sufficiency. An attacker can craft structurally valid transactions referencing non-existent inputs; these pass the structural gate, occupy queue space, and are only rejected later by workers. The cost is network bandwidth alone.
- **Sustainability**: As workers reject queued transactions and free space, the attacker immediately refills it. The attack loop is cheap and continuous.
- **No per-peer quota**: Nothing in `VerifyQueue` or the relay handler limits how many bytes a single peer may contribute to the global queue.

---

### Recommendation

**Short term:** Introduce a per-peer byte quota inside `VerifyQueue`. Track each `PeerIndex`'s contribution to `total_tx_size` and reject additions that would exceed the per-peer cap, analogous to how `remove_txs_by_peer` already tracks peer ownership.

**Long term:** Apply rate limiting at the relay-protocol layer (e.g., in the `RelayTransactions` handler) so that a single peer cannot submit transactions faster than workers can drain them. Regularly review the `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` constant relative to `max_tx_verify_cycles` and worker count to ensure the queue cannot be held full by a realistic number of peers.

---

### Proof of Concept

1. Connect to a target CKB node as a standard P2P peer.
2. Craft ~512 transactions of ~512 KB each that are structurally valid (pass `NonContextualTransactionVerifier`) but reference non-existent input cells.
3. Relay all transactions via `RelayTransactions` messages. Each passes `non_contextual_verify` and is inserted into the `VerifyQueue`, incrementing `total_tx_size` toward 256 MB.
4. Once `total_tx_size ≥ DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`, every subsequent `add_tx` call returns `Reject::Full`. All RPC `send_transaction` calls and all P2P-relayed transactions from legitimate users are rejected.
5. As workers drain the queue (rejecting the attacker's transactions at the resolve step), immediately relay a new batch to keep `total_tx_size` at the ceiling. The node remains unable to accept any user transactions for as long as the attacker maintains the connection and the submission rate.

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

**File:** tx-pool/src/component/verify_queue.rs (L158-168)
```rust
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
```

**File:** tx-pool/src/component/verify_queue.rs (L215-219)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
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

**File:** util/types/src/core/tx_pool.rs (L305-309)
```rust
/// The maximum size of the tx-pool to accept transactions
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
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
