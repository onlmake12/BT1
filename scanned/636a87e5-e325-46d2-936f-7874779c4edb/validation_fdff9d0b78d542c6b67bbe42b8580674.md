Audit Report

## Title
Verify Queue Flooded with Semantically Invalid Transactions Delays Legitimate Transaction Processing - (File: `tx-pool/src/component/verify_queue.rs`, `tx-pool/src/process.rs`)

## Summary

CKB's `VerifyQueue` admits transactions after only a structural (`non_contextual_verify`) check, deferring fee validity and input existence checks to a background worker that runs after the transaction already occupies queue space. An unprivileged remote peer can flood the 256 MB queue with structurally valid but semantically invalid transactions (e.g., referencing non-existent `OutPoint`s), causing legitimate transactions to be rejected with `Reject::Full` or delayed until the attacker's batch is drained. Missing-input rejections do not trigger peer banning, so the attacker peer remains connected and can repeat the attack indefinitely.

## Finding Description

**Admission path (no semantic check):**

`submit_remote_tx` → `resumeble_process_tx` → `non_contextual_verify` → `enqueue_verify_queue`

`non_contextual_verify` in `tx-pool/src/util.rs` runs `NonContextualTransactionVerifier`, which checks only version, size, empty inputs/outputs, duplicate deps, outputs data length, and script hash type. It performs no input existence check and no fee check. [1](#0-0) 

After passing this gate, the transaction is inserted into the queue via `add_tx`, which enforces only a single admission condition: total serialized size must not exceed `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` bytes. [2](#0-1) [3](#0-2) [4](#0-3) 

**Semantic checks happen post-admission:**

`_process_tx` calls `pre_check`, which calls `resolve_tx` (input existence) and `check_tx_fee` (fee validity), only after the transaction has been dequeued by the background worker. [5](#0-4) [6](#0-5) 

**No ban on missing-input rejection:**

In `after_process`, when `is_missing_input(reject)` is true, the transaction is routed to the orphan pool. `ban_malformed` is never called for this path. Only `is_malformed_tx()` rejections trigger a ban. [7](#0-6) [8](#0-7) 

**Relay-level rate limiter is insufficient:**

The relay protocol applies a rate limiter of 30 `RelayTransactions` messages per second per `(PeerIndex, message_type)` pair. Each `RelayTransactions` message may carry up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` transactions and `MAX_RELAY_TXS_BYTES_PER_BATCH = 1 MB`. At ~200 bytes per transaction, this allows ~150,000 transactions per second per peer, sufficient to fill the 256 MB queue in seconds. [9](#0-8) 

The relay-level filter in `TransactionsProcess` requires that the node previously requested the tx hashes via `GetRelayTransactions`, but the attacker can satisfy this by first announcing fake hashes via `RelayTransactionHashes`, causing the node to request them, then delivering the semantically invalid transactions. [10](#0-9) 

**Queue full rejection:**

When the queue is full, new transactions receive `Reject::Full` and are dropped. [4](#0-3) [11](#0-10) 

**FIFO processing order:**

Workers dequeue entries strictly in `added_time` order via `pop_front` → `peek`, so legitimate transactions submitted after the attacker's batch must wait for all attacker entries to be processed and rejected. [12](#0-11) 

## Impact Explanation

This matches the **High** impact category: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

An attacker who fills the verify queue on multiple nodes simultaneously can:
1. Cause `Reject::Full` for all legitimate transactions submitted during the flood window.
2. Force legitimate transactions to wait for the entire attacker batch to be drained (FIFO ordering).
3. Compound resource pressure by routing rejected transactions to the orphan pool, which has its own separate size limit.
4. Repeat the attack immediately after the worker drains the queue, with no cooldown enforced.

The attack requires no on-chain funds, no privileged access, and no majority hashpower.

## Likelihood Explanation

- **Entry path**: Any unprivileged remote peer via the standard relay protocol (`submit_remote_tx`). No key material or special privileges required.
- **Cost**: Crafting structurally valid transactions with fake `OutPoint`s requires only CPU and network bandwidth. No on-chain funds are needed.
- **Relay flow**: The attacker must go through the announce (`RelayTransactionHashes`) → request (`GetRelayTransactions`) → deliver (`RelayTransactions`) flow, but this is the standard relay protocol and imposes no meaningful barrier.
- **Rate limiter**: The 30 msg/sec relay rate limiter is insufficient given the 32,767 tx/message batch size.
- **Repeatability**: After the worker drains the queue, the attacker can immediately re-flood. No per-peer admission throttling exists at the verify queue level, and no ban is issued for missing-input rejections.

## Recommendation

1. **Enforce a minimum fee rate at queue admission**: Before calling `enqueue_verify_queue`, perform a lightweight fee-rate plausibility check using declared output capacity. This rejects zero-fee or below-minimum-fee transactions before they occupy queue space.
2. **Per-peer queue slot limit**: Track bytes per remote peer in the verify queue. Reject new submissions from a peer once their share exceeds a configurable threshold (e.g., `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / max_peers`).
3. **Priority eviction**: Allow the queue to evict the lowest-fee-rate entry when full and a higher-fee-rate entry arrives, rather than rejecting the new entry outright.
4. **Ban on repeated missing-input rejections**: Track per-peer missing-input rejection counts and apply a ban after a threshold is exceeded, analogous to the existing `ban_malformed` logic.

## Proof of Concept

1. Connect to a CKB node as a peer.
2. Announce ~1.28 million fake tx hashes via `RelayTransactionHashes` (batched across multiple messages at 30/sec).
3. When the node responds with `GetRelayTransactions`, deliver structurally valid transactions (each ~200 bytes) spending randomly generated, non-existent `OutPoint`s. These pass `non_contextual_verify` and are admitted to the verify queue.
4. The verify queue reaches its 256 MB limit.
5. A legitimate user submits a transaction via RPC or relay. They receive `Reject::Full`.
6. The background worker begins draining the attacker's transactions. Each is rejected at `pre_check` for `OutPointError::Unknown` and routed to the orphan pool. The orphan pool fills, evicting legitimate orphans.
7. The attacker's peer is never banned (missing-input rejections do not trigger `ban_malformed`).
8. After the queue drains, the attacker immediately re-floods.

**Confirming code path — no fee/input check at admission:** [13](#0-12) [14](#0-13)

### Citations

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

**File:** tx-pool/src/component/verify_queue.rs (L171-194)
```rust
    pub fn pop_front(&mut self, only_small_cycle: bool) -> Option<Entry> {
        if let Some(short_id) = self.peek(only_small_cycle) {
            self.remove_tx(&short_id)
        } else {
            None
        }
    }

    /// Returns the first entry in the queue
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

**File:** tx-pool/src/process.rs (L286-290)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
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

**File:** tx-pool/src/process.rs (L355-369)
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
```

**File:** tx-pool/src/process.rs (L507-515)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
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

**File:** sync/src/relayer/mod.rs (L59-99)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;

type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }
```

**File:** sync/src/relayer/transactions_process.rs (L39-57)
```rust
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };
```
