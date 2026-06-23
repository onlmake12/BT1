### Title
No Rate Limiting on `get_transaction` RPC Handler Acquires Shared Tx-Pool Lock and Makes Database Lookups - (File: rpc/src/module/chain.rs)

### Summary

The `get_transaction` RPC method in `rpc/src/module/chain.rs` has no rate limiting at any layer. When called without `only_committed=true` (the default), it sends a synchronous blocking message to the single-threaded tx-pool service actor, which acquires a read lock on the shared tx-pool, queries the `recent_reject` database, and triggers `error!()` logging on failure. An unprivileged RPC caller can flood this endpoint to saturate the tx-pool service message queue, delaying or blocking critical operations such as transaction submission and block template generation.

### Finding Description

The CKB RPC server (`rpc/src/server.rs`) has no rate limiting of any kind — a grep for `rate_limit`, `rate_limiter`, `RateLimiter`, or `throttle` in `rpc/**/*.rs` returns zero matches. [1](#0-0) 

The `get_transaction` RPC handler in `ChainRpcImpl` dispatches to either `get_transaction_verbosity1` or `get_transaction_verbosity2`. Both, when `only_committed` is `false` (the default), call into the tx-pool controller: [2](#0-1) 

`get_transaction_verbosity2` calls `tx_pool.get_transaction_with_status(tx_hash)`, which sends a `Message::GetTransactionWithStatus` to the tx-pool service actor: [3](#0-2) 

Inside the tx-pool service, handling `GetTransactionWithStatus` acquires `service.tx_pool.read().await` (a shared async read lock on the entire pool map), then queries the `recent_reject` RocksDB database: [4](#0-3) 

The tx-pool service is a single-threaded actor that processes messages sequentially from a bounded channel. All critical operations — `SubmitLocalTx`, `BlockTemplate`, `GetLiveCell`, `ClearPool` — share this same queue: [5](#0-4) 

On any channel send or responder failure, `error!()` is emitted: [6](#0-5) 

### Impact Explanation

An attacker flooding `get_transaction` (with default `only_committed=false`) saturates the tx-pool service message queue. Because the actor processes messages one at a time, queued `GetTransactionWithStatus` messages delay or block:

- `SubmitLocalTx` / `SubmitRemoteTx` — legitimate transaction submissions stall
- `BlockTemplate` — miners cannot obtain fresh block templates, degrading block production
- `GetLiveCell` (with `include_tx_pool=true`) — same queue contention

Each request also acquires `tx_pool.read()` and performs a RocksDB lookup on `recent_reject`, consuming I/O and memory resources. Repeated failures produce sustained `error!()` log noise that can obscure real operational errors.

### Likelihood Explanation

The RPC endpoint is reachable by any client that can connect to the node's HTTP RPC port. While the default bind is `127.0.0.1:8114`, many operators expose it publicly or via proxies. No authentication is required. The attacker needs only to send repeated JSON-RPC POST requests with arbitrary `tx_hash` values. The cost per request is negligible for the attacker but non-trivial for the node.

### Recommendation

1. Implement per-IP or global rate limiting on the RPC server layer (e.g., via a Tower middleware on the Axum router in `rpc/src/server.rs`).
2. For `get_transaction`, add a fast path: when `only_committed=true`, the tx-pool is never consulted. Encourage or default to this flag for read-heavy use cases.
3. Consider a separate bounded channel or priority queue for read-only tx-pool queries vs. write operations, so query flooding cannot starve `SubmitLocalTx` or `BlockTemplate`.

### Proof of Concept

```
# Flood get_transaction with random hashes (no authentication needed)
for i in $(seq 1 100000); do
  curl -s -X POST http://<node>:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"get_transaction","params":["0x'$(openssl rand -hex 32)'"]}' &
done

# Simultaneously attempt a legitimate transaction submission:
curl -X POST http://<node>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"send_transaction","params":[<valid_tx>,"passthrough"]}'
# The send_transaction will be delayed or time out due to tx-pool queue saturation.
```

The `get_transaction` call path without `only_committed=true` unconditionally enqueues a `GetTransactionWithStatus` message to the tx-pool actor, acquiring `tx_pool.read().await` and querying `recent_reject` for every request, with no throttle at any layer. [7](#0-6)

### Citations

**File:** rpc/src/server.rs (L218-299)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
        Json(jsonrpc_core::Failure {
            jsonrpc: Some(jsonrpc_core::Version::V2),
            id: jsonrpc_core::Id::Null,
            error,
        })
        .into_response()
    };

    let req = match std::str::from_utf8(req_body.as_ref()) {
        Ok(req) => req,
        Err(_) => {
            return make_error_response(jsonrpc_core::Error::parse_error());
        }
    };

    let req = serde_json::from_str::<Request>(req);
    match req {
        Err(_error) => {
            let response = RpcResponse::from(
                Error::new(ErrorCode::ParseError),
                Some(jsonrpc_core::Version::V2),
            );

            serde_json::to_string(&response)
                .map(|json| {
                    (
                        [(axum::http::header::CONTENT_TYPE, "application/json")],
                        json,
                    )
                        .into_response()
                })
                .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
        Ok(request) => match request {
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;

                if let Some(response) = result {
                    serde_json::to_string(&response)
                        .map(|json| {
                            (
                                [(axum::http::header::CONTENT_TYPE, "application/json")],
                                json,
                            )
                                .into_response()
                        })
                        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
                } else {
                    StatusCode::NO_CONTENT.into_response()
                }
            }
            Request::Batch(calls) => {
                if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
                    && calls.len() > *batch_size
                {
                    return make_error_response(jsonrpc_core::Error::invalid_params(format!(
                        "batch size is too large, expect it less than: {}",
                        batch_size
                    )));
                }

                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });

                (
                    [(axum::http::header::CONTENT_TYPE, "application/json")],
                    StreamBodyAs::json_array(stream),
                )
                    .into_response()
            }
        },
    }
}
```

**File:** rpc/src/module/chain.rs (L1749-1780)
```rust
    fn get_transaction(
        &self,
        tx_hash: H256,
        verbosity: Option<Uint32>,
        only_committed: Option<bool>,
    ) -> Result<TransactionWithStatusResponse> {
        let tx_hash = tx_hash.into();
        let verbosity = verbosity
            .map(|v| v.value())
            .unwrap_or(DEFAULT_GET_TRANSACTION_VERBOSITY_LEVEL);

        let only_committed: bool = only_committed.unwrap_or(false);

        if verbosity == 0 {
            // when verbosity=0, it's response value is as same as verbosity=2, but it
            // return a 0x-prefixed hex encoded molecule packed::Transaction` on `transaction` field
            self.get_transaction_verbosity2(tx_hash, only_committed)
                .map(|tws| TransactionWithStatusResponse::from(tws, ResponseFormatInnerType::Hex))
        } else if verbosity == 1 {
            // The RPC does not return the transaction content and the field transaction must be null.
            self.get_transaction_verbosity1(tx_hash, only_committed)
                .map(|tws| TransactionWithStatusResponse::from(tws, ResponseFormatInnerType::Json))
        } else if verbosity == 2 {
            // if tx_status.status is pending, proposed, or committed,
            // the RPC returns the transaction content as field transaction,
            // otherwise the field is null.
            self.get_transaction_verbosity2(tx_hash, only_committed)
                .map(|tws| TransactionWithStatusResponse::from(tws, ResponseFormatInnerType::Json))
        } else {
            Err(RPCError::invalid_params("invalid verbosity level"))
        }
    }
```

**File:** rpc/src/module/chain.rs (L2185-2232)
```rust
    fn get_transaction_verbosity2(
        &self,
        tx_hash: packed::Byte32,
        only_committed: bool,
    ) -> Result<TransactionWithStatus> {
        let snapshot = self.shared.snapshot();
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
        if let Err(e) = transaction_with_status {
            error!("Send get_transaction_with_status request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        };
        let transaction_with_status = transaction_with_status.unwrap();

        if let Err(e) = transaction_with_status {
            error!("Get transaction_with_status from db error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        };
        let transaction_with_status = transaction_with_status.unwrap();
        Ok(transaction_with_status)
    }
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
