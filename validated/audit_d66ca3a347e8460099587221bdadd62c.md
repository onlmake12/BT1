Audit Report

## Title
No Rate Limiting on `get_transaction` RPC Handler Saturates Shared Tx-Pool Actor Queue - (File: rpc/src/module/chain.rs)

## Summary
The `get_transaction` RPC endpoint has no rate limiting at any layer. When called with the default `only_committed=false`, it unconditionally enqueues a `GetTransactionWithStatus` message to the single-threaded tx-pool service actor, which acquires a shared async read lock on the pool and performs a RocksDB lookup. An unprivileged attacker can flood this endpoint to saturate the bounded actor queue, delaying or blocking `BlockTemplate`, `SubmitLocalTx`, and `SubmitRemoteTx` operations that share the same queue.

## Finding Description
The RPC server in `rpc/src/server.rs` (L119-129) builds an Axum router with only a `CorsLayer` and a `TimeoutLayer` (30s). No rate limiting, connection limiting, or concurrency semaphore is present — a grep for `rate_limit`, `rate_limiter`, `RateLimiter`, `throttle`, `max_connections`, `connection_limit`, `semaphore`, or `concurrency_limit` in `rpc/**/*.rs` returns zero matches. [1](#0-0) 

In `rpc/src/module/chain.rs` (L1760), `only_committed` defaults to `false`. When the transaction is not found in the committed chain, `get_transaction_verbosity2` (L2214-2219) skips the early return and calls `tx_pool.get_transaction_with_status(tx_hash)`, enqueuing a `Message::GetTransactionWithStatus` to the tx-pool actor. [2](#0-1) [3](#0-2) 

Inside the tx-pool service actor (`tx-pool/src/service.rs` L904-942), handling `GetTransactionWithStatus` acquires `service.tx_pool.read().await` (a shared async read lock on the entire pool map) and then queries the `recent_reject` RocksDB database. The actor processes messages sequentially in a single loop. [4](#0-3) 

All critical operations — `BlockTemplate`, `SubmitLocalTx`, `SubmitRemoteTx`, `GetLiveCell`, `ClearPool` — share this same `Message` enum and thus the same bounded channel queue. [5](#0-4) 

An attacker flooding `get_transaction` with random hashes (which will never be found in the committed chain) keeps the actor queue saturated with `GetTransactionWithStatus` messages. Each message holds the read lock while performing a RocksDB lookup. `SubmitLocalTx` and `BlockTemplate` messages queued behind them are delayed until all flood messages are drained. The 30-second `TimeoutLayer` means each attacker request occupies a queue slot for up to 30 seconds.

## Impact Explanation
Targeting a mining node's RPC endpoint (which many operators expose publicly or via proxies) allows an attacker to block `BlockTemplate` responses, preventing miners from obtaining fresh block templates. This degrades block production across targeted mining infrastructure. The cost to the attacker is negligible (unauthenticated HTTP POST requests with random hashes), while the impact on the node is sustained queue saturation and I/O load. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The RPC endpoint requires no authentication. The default bind is `127.0.0.1:8114`, but many operators expose it publicly or behind proxies. The attacker needs only to send concurrent JSON-RPC POST requests with arbitrary 32-byte hashes. Random hashes will never match committed transactions, ensuring every request reaches the tx-pool actor path. The attack is trivially scriptable and repeatable.

## Recommendation
1. Add per-IP or global rate limiting middleware (e.g., `tower_governor` or a custom Tower layer) to the Axum router in `rpc/src/server.rs` before the `handle_jsonrpc` handler.
2. Add a connection concurrency limit (e.g., `tower::limit::ConcurrencyLimitLayer`) to bound the number of simultaneous in-flight RPC requests.
3. Consider a separate bounded channel or priority queue for read-only tx-pool queries (`GetTransactionWithStatus`, `GetTxStatus`) vs. write/critical operations (`SubmitLocalTx`, `BlockTemplate`), so query flooding cannot starve block template generation.
4. For `get_transaction`, document and encourage `only_committed=true` for read-heavy use cases, which bypasses the tx-pool actor entirely.

## Proof of Concept
```bash
# Flood get_transaction with random hashes (no authentication needed)
for i in $(seq 1 10000); do
  curl -s -X POST http://<node>:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"get_transaction","params":["0x'$(openssl rand -hex 32)'"]}' &
done

# Simultaneously attempt block template generation (miner path):
curl -X POST http://<node>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"get_block_template","params":[null,null,null]}'
# Expected: get_block_template is delayed or times out due to tx-pool queue saturation.
```

Each flood request with a random hash bypasses the committed-chain lookup (L2191), hits the `only_committed=false` path (L2214), and enqueues a `GetTransactionWithStatus` message that acquires `tx_pool.read().await` and queries RocksDB (L909-934), with no throttle at any layer. [6](#0-5) [7](#0-6)

### Citations

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** rpc/src/module/chain.rs (L1760-1760)
```rust
        let only_committed: bool = only_committed.unwrap_or(false);
```

**File:** rpc/src/module/chain.rs (L2191-2219)
```rust
        if let Some((tx, tx_info)) = snapshot.get_transaction_with_info(&tx_hash) {
            let cycles = if tx_info.is_cellbase() {
                None
            } else {
                snapshot
                    .get_block_ext(&tx_info.block_hash)
                    .and_then(|block_ext| {
                        block_ext
                            .cycles
                            .and_then(|v| v.get(tx_info.index.saturating_sub(1)).copied())
                    })
            };

            return Ok(TransactionWithStatus::with_committed(
                Some(tx),
                tx_info.block_number,
                tx_info.block_hash.into(),
                tx_info.index as u32,
                cycles,
                None,
            ));
        }

        if only_committed {
            return Ok(TransactionWithStatus::with_unknown());
        }

        let tx_pool = self.shared.tx_pool_controller();
        let transaction_with_status = tx_pool.get_transaction_with_status(tx_hash);
```

**File:** tx-pool/src/service.rs (L117-149)
```rust
pub(crate) enum Message {
    BlockTemplate(Request<BlockTemplateArgs, BlockTemplateResult>),
    SubmitLocalTx(Request<TransactionView, SubmitTxResult>),
    RemoveLocalTx(Request<Byte32, bool>),
    TestAcceptTx(Request<TransactionView, TestAcceptTxResult>),
    SubmitRemoteTx(Request<(TransactionView, Cycle, PeerIndex), ()>),
    NotifyTxs(Notify<Vec<TransactionView>>),
    FreshProposalsFilter(AsyncRequest<Vec<ProposalShortId>, Vec<ProposalShortId>>),
    FetchTxs(AsyncRequest<HashSet<ProposalShortId>, HashMap<ProposalShortId, TransactionView>>),
    FetchTxsWithCycles(AsyncRequest<HashSet<ProposalShortId>, FetchTxsWithCyclesResult>),
    GetTxPoolInfo(Request<(), TxPoolInfo>),
    GetLiveCell(Request<(OutPoint, bool), CellStatus>),
    GetTxStatus(Request<Byte32, GetTxStatusResult>),
    GetTransactionWithStatus(Request<Byte32, GetTransactionWithStatusResult>),
    NewUncle(Notify<UncleBlockView>),
    ClearPool(Request<Arc<Snapshot>, ()>),
    ClearVerifyQueue(Request<(), ()>),
    GetAllEntryInfo(Request<(), TxPoolEntryInfo>),
    GetAllIds(Request<(), TxPoolIds>),
    SavePool(Request<(), ()>),
    GetPoolTxDetails(Request<Byte32, PoolTxDetailInfo>),
    GetTotalRecentRejectNum(Request<(), Option<u64>>),

    UpdateIBDState(Request<bool, ()>),
    EstimateFeeRate(Request<(EstimateMode, bool), FeeEstimatesResult>),

    // test
    #[cfg(feature = "internal")]
    PlugEntry(Request<(Vec<TxEntry>, PlugTarget), ()>),
    #[cfg(feature = "internal")]
    PackageTxs(Request<Option<u64>, Vec<TxEntry>>),
    SubmitLocalTestTx(Request<TransactionView, SubmitTxResult>),
}
```

**File:** tx-pool/src/service.rs (L904-942)
```rust
        Message::GetTransactionWithStatus(Request {
            responder,
            arguments: hash,
        }) => {
            let id = ProposalShortId::from_tx_hash(&hash);
            let tx_pool = service.tx_pool.read().await;
            let ret = if let Some(PoolEntry {
                status,
                inner: entry,
                ..
            }) = tx_pool.pool_map.get_by_id(&id)
            {
                let (tx_status, min_replace_fee) = if status == &Status::Proposed {
                    (TxStatus::Proposed, None)
                } else {
                    (TxStatus::Pending, tx_pool.min_replace_fee(entry))
                };
                Ok(TransactionWithStatus::with_status(
                    Some(entry.transaction().clone()),
                    entry.cycles,
                    entry.timestamp,
                    tx_status,
                    Some(entry.fee),
                    min_replace_fee,
                ))
            } else if let Some(ref recent_reject_db) = tx_pool.recent_reject {
                match recent_reject_db.get(&hash) {
                    Ok(Some(record)) => Ok(TransactionWithStatus::with_rejected(record)),
                    Ok(_) => Ok(TransactionWithStatus::with_unknown()),
                    Err(err) => Err(err),
                }
            } else {
                Ok(TransactionWithStatus::with_unknown())
            };

            if let Err(e) = responder.send(ret) {
                error!("Responder sending get_tx_status failed {:?}", e)
            };
        }
```
