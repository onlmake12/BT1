Audit Report

## Title
Queue Admission Without Fee Check Enables Low-Cost DoS of Transaction Pool — (File: `tx-pool/src/process.rs`)

## Summary
The CKB tx-pool's `resumeble_process_tx` admits transactions into the 256 MB verify queue after only a structural `non_contextual_verify` check, with no fee-rate gate. The fee check (`check_tx_fee`) only runs later in `_process_tx → pre_check` after a worker dequeues the entry. Any unprivileged P2P peer can exploit the standard relay protocol to flood the verify queue with structurally valid, zero-fee transactions containing fake inputs, saturating the 256 MB limit and causing all subsequent legitimate transactions to be rejected with `Reject::Full`.

## Finding Description

**Admission path — no fee check before enqueue:**

`resumeble_process_tx` calls `non_contextual_verify`, checks for duplicates, then immediately calls `enqueue_verify_queue` with no fee gate: [1](#0-0) 

`non_contextual_verify` only validates structural properties (version via `NonContextualTransactionVerifier`, size ≤ `TRANSACTION_SIZE_LIMIT`, not a cellbase). It does not verify input existence or fee rate: [2](#0-1) 

**Fee check is deferred to the worker stage:**

`pre_check` (called by `_process_tx` after dequeue) is where `check_tx_fee` runs. It requires a fully resolved `ResolvedTransaction` obtained via `resolve_tx`, which can only succeed after the transaction has already occupied verify queue memory: [3](#0-2) 

`check_tx_fee` compares the actual fee against `min_fee_rate * tx_size`, but by this point the transaction already occupies verify queue memory: [4](#0-3) 

**Verify queue hard limit is 256 MB; full queue rejects all new entries:** [5](#0-4) [6](#0-5) 

**Relay path is reachable by any unprivileged peer:**

The relay handler in `transactions_process.rs` admits transactions that (a) are not already known and (b) were previously requested from the sending peer. An attacker satisfies (b) by first sending `RelayTransactionHashes`, causing the node to issue `GetRelayTransactions`, then responding with crafted transactions. The only ban in the relay handler is for `declared_cycles > max_block_cycles`, which the attacker avoids by declaring a valid cycle count: [7](#0-6) 

The relay handler submits via `submit_remote_tx`, which calls `resumeble_process_tx_and_notify_full_reject` → `resumeble_process_tx` — no fee check before enqueue: [8](#0-7) 

**No banning for low-fee or unresolvable transactions:**

`ban_malformed` is only triggered for `is_malformed_tx()` rejects. `Reject::LowFeeRate` and `Reject::Resolve` (for fake inputs) are not malformed rejects, so the attacker peer is never banned and `remove_txs_by_peer` is never called for their entries: [9](#0-8) 

## Impact Explanation

While the verify queue is saturated (256 MB), every new transaction submitted via RPC or relayed from other peers is rejected with `Reject::Full`. Workers drain the queue over time (resolve fails for fake inputs, or `check_tx_fee` fails for zero fee), but the attacker can immediately re-flood using new random input `OutPoint`s. This constitutes sustained, repeatable disruption of the transaction relay and submission pipeline. Impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

Any unprivileged P2P peer can execute this attack using the standard relay protocol with no special keys, privileges, or hashpower. The attacker's cost is network bandwidth and CPU to craft ~500 structurally valid transactions (each up to 512 KB). The attack is repeatable and can be sustained indefinitely as long as the attacker maintains a peer connection, since the peer is never banned.

## Recommendation

Move the fee-rate pre-check to before `enqueue_verify_queue` in `resumeble_process_tx`. A lightweight check using only the transaction's serialized size (available without input resolution) and a conservative fee estimate can gate admission. In `tx-pool/src/process.rs`, before calling `self.enqueue_verify_queue(...)`, compute `min_fee = min_fee_rate.fee(tx.data().serialized_size_in_block())` and reject if the declared fee falls below the threshold. For remote transactions, the declared cycles and outputs capacity are available without full resolution. Alternatively, add a minimum fee-rate gate inside `enqueue_verify_queue` itself, or rate-limit enqueue operations per peer.

## Proof of Concept

```
1. Attacker peer connects to CKB node via P2P.
2. Attacker sends RelayTransactionHashes([H1, H2, ..., H500])
   where H_i = hash(crafted_tx_i).
3. Node adds H1..H500 to unknown_tx_hashes and sends GetRelayTransactions([H1..H500]).
4. Attacker sends RelayTransactions([crafted_tx_1, ..., crafted_tx_500]) where each:
     - inputs: [OutPoint { tx_hash: random_32_bytes, index: 0 }]  ← fake, non-existent
     - outputs: [CellOutput { capacity: 0, lock: always_success_script }]
     - witnesses: [minimal valid witness]
     - serialized_size ≈ 512 KB (TRANSACTION_SIZE_LIMIT)
     - declared_cycles: 1  ← avoids max_block_cycles ban
     - fee: 0 shannons
5. Each tx passes non_contextual_verify (structural check only, no fee/input check).
6. Each tx is enqueued: 500 × 512 KB ≈ 256 MB → verify queue full.
7. All subsequent send_transaction RPC calls and peer relay submissions return Reject::Full.
8. Workers drain queue (resolve_tx fails for fake inputs, or check_tx_fee fails for zero fee).
9. Attacker immediately re-floods from step 2 using new random input OutPoints.
   Peer is never banned (LowFeeRate/Resolve rejects are not malformed rejects).
```

### Citations

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
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

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
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

**File:** sync/src/relayer/transactions_process.rs (L37-96)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
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

        if txs.is_empty() {
            return Status::ok();
        }

        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }

        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });

        Status::ok()
    }
```
