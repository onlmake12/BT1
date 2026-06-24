Audit Report

## Title
Single Peer Can Monopolize the Shared VerifyQueue Budget, Blocking All Remote Transaction Submissions — (File: `tx-pool/src/component/verify_queue.rs`)

## Summary
`VerifyQueue` enforces a single global byte-budget (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256 MB`) with no per-peer sub-limit. A single connected peer can fill the entire queue by delivering large transactions via the standard two-step relay protocol. Once saturated, every `submit_remote_tx` call from any peer returns `Reject::Full`, blocking all honest remote transaction submissions until the queue drains.

## Finding Description

**Global-only budget in `VerifyQueue`:**

`VerifyQueue` tracks a single `total_tx_size` counter with no per-peer accounting. [1](#0-0) 

`is_full()` compares only against the global constant: [2](#0-1) 

`add_tx()` returns `Reject::Full` for every caller once this global limit is reached, regardless of which peer contributed the blocking bytes: [3](#0-2) 

**Admission path — `non_contextual_verify` does not check input validity:**

`non_contextual_verify` only checks structure, size ≤ `TRANSACTION_SIZE_LIMIT`, and non-cellbase. Transactions with non-existent inputs pass this check: [4](#0-3) 

Input resolution (`resolve_tx` / `pre_check`) happens inside the queue worker after dequeue, not before admission: [5](#0-4) 

The enqueue step is reached after `non_contextual_verify` passes: [6](#0-5) 

**Two-step relay protocol is standard and sufficient:**

The `requesting_peer` filter in `TransactionsProcess::execute()` is satisfied by the normal relay flow — announce hashes, wait for `GetRelayTransactions`, deliver transactions: [7](#0-6) 

The `ask_for_txs` timer fires every 100 ms: [8](#0-7) 

**Existing guards are insufficient:**

The rate limiter is keyed by `(PeerIndex, message_type)` at 30 requests/second — it limits message count, not bytes per message: [9](#0-8) 

`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` far exceeds the ~512 transactions needed to fill 256 MB at 512 KB each: [10](#0-9) 

`remove_txs_by_peer` is only called inside `ban_malformed`. For transactions with non-existent inputs, the rejection is `Reject::Resolve(OutPointError::Unknown)` — `is_missing_input()` is true, so the tx goes to the orphan pool and `ban_malformed` is never triggered: [11](#0-10) 

The `disconnected` handler in the relayer does not call `remove_txs_by_peer`: [12](#0-11) 

## Impact Explanation

When the verify queue is full, every call to `submit_remote_tx` returns `Reject::Full`. The tx-pool service propagates this rejection back to the relayer, which marks the transaction as unknown and stops requesting it. Honest peers' transactions are silently dropped at the queue boundary — never verified, never entering the pending pool, never mined. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." An attacker targeting multiple nodes simultaneously can degrade transaction propagation across the network.

## Likelihood Explanation

The attack requires only a single connected peer with no special privileges. The attacker needs syntactically valid transactions (passing `non_contextual_verify`: correct structure, size ≤ `TRANSACTION_SIZE_LIMIT`, not cellbase) but does not need valid scripts or real inputs. The two-step protocol (announce hashes → receive request → deliver txs) is standard relay behavior. At 30 `RelayTransactions` messages/second, each carrying multiple large transactions, the 512-transaction threshold is reachable in seconds. The attacker can reconnect after a disconnect to re-fill the queue, since `remove_txs_by_peer` is not called on disconnect and the queue drains only as the worker processes entries.

## Recommendation

Track per-peer byte usage inside `VerifyQueue`. Add a `HashMap<PeerIndex, usize>` field for peer byte totals. In `add_tx()`, enforce a per-peer cap (e.g., `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / expected_max_peers`) so that no single peer can consume more than its fair share. When a peer's quota is exhausted, return `Reject::Full` only for that peer's submissions rather than blocking all peers. Update `remove_tx()` and `remove_txs_by_peer()` to decrement the per-peer counter accordingly.

## Proof of Concept

1. Connect to a CKB node as a relay peer (`SupportProtocols::RelayV3`).
2. Construct 512 syntactically valid transactions, each close to `TRANSACTION_SIZE_LIMIT`, referencing non-existent inputs (so they fail script verification inside the queue but are admitted to it first).
3. Send `RelayTransactionHashes` messages announcing the hashes of these transactions. The node adds them to `unknown_tx_hashes` (limit `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` >> 512).
4. Wait for the node's `ask_for_txs()` timer (fires every 100 ms) to send `GetRelayTransactions` back to the attacker.
5. Respond with `RelayTransactions` messages carrying the large transactions. Each passes `non_contextual_verify` and the `requesting_peer` filter, then enters `enqueue_verify_queue`.
6. Observe via `get_tip_tx_pool_info` that `verify_queue_size` climbs to its maximum.
7. Attempt to submit a legitimate transaction from any other peer or via RPC `send_transaction`; it is rejected with `Reject::Full("verify_queue total_tx_size exceeded …")`.
8. Repeat from step 3 after disconnect to maintain saturation.

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L56-65)
```rust
pub(crate) struct VerifyQueue {
    /// inner tx entry
    inner: MultiIndexVerifyEntryMap,
    /// subscribe this notify to get be notified when there is item in the queue
    ready_rx: Arc<Notify>,
    /// total tx size in the queue, will reject new transaction if exceed the limit
    total_tx_size: usize,
    /// large cycle threshold, from `pool_config.max_tx_verify_cycles`
    large_cycle_threshold: u64,
}
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
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

**File:** tx-pool/src/process.rs (L679-703)
```rust
    async fn ban_malformed(&self, peer: PeerIndex, reason: String) {
        const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);

        #[cfg(feature = "with_sentry")]
        use sentry::{Level, capture_message, with_scope};

        #[cfg(feature = "with_sentry")]
        with_scope(
            |scope| scope.set_fingerprint(Some(&["ckb-tx-pool", "receive-invalid-remote-tx"])),
            || {
                capture_message(
                    &format!(
                        "Ban peer {} for {} seconds, reason: \
                        {}",
                        peer,
                        DEFAULT_BAN_TIME.as_secs(),
                        reason
                    ),
                    Level::Info,
                )
            },
        );
        self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
        self.verify_queue.write().await.remove_txs_by_peer(&peer);
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

**File:** sync/src/relayer/transactions_process.rs (L49-55)
```rust
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L799-802)
```rust
            .await
            .expect("set_notify at init is ok");
        nc.set_notify(Duration::from_millis(100), ASK_FOR_TXS_TOKEN)
            .await
```

**File:** sync/src/relayer/mod.rs (L923-935)
```rust
    async fn disconnected(
        &mut self,
        _nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
    ) {
        info_target!(
            crate::LOG_TARGET_RELAY,
            "RelayProtocol.disconnected peer={}",
            peer_index
        );
        // Retains all keys in the rate limiter that were used recently enough.
        self.rate_limiter.retain_recent();
    }
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
