### Title
Unbounded JSON-RPC Batch Request Processing Enables Node-Level DoS — (`rpc/src/server.rs`)

### Summary
The CKB JSON-RPC server has two compounding input-parsing weaknesses: (1) the `max_request_body_size` configuration field is read from config but never applied to the axum HTTP router, so the operator-configured body limit is silently ignored; and (2) the batch-request size guard is only active when `rpc_batch_limit` is explicitly set, which it is not by default. Any caller with RPC access can therefore send a single batch request containing thousands of method calls, forcing the node to process all of them sequentially and exhausting CPU and memory until the node becomes unresponsive.

### Finding Description

**Root cause 1 — `max_request_body_size` is never applied**

`RpcServer::new` reads `config.max_request_body_size` but passes only `config.listen_address` to `start_server`. No `tower_http::limit::RequestBodyLimitLayer` or `axum::extract::DefaultBodyLimit` layer is added to the router. The axum default body limit (2 MiB) therefore applies unconditionally, regardless of what the operator configured. [1](#0-0) [2](#0-1) [3](#0-2) 

The default config documents `max_request_body_size = 10485760` (10 MiB) and operators rely on it, but the value is never consumed in the server setup path. [4](#0-3) 

**Root cause 2 — batch size limit is opt-in and disabled by default**

The static `JSONRPC_BATCH_LIMIT` is only initialised when `config.rpc_batch_limit` is `Some`. The default configuration leaves `rpc_batch_limit` commented out, so `JSONRPC_BATCH_LIMIT.get()` returns `None` and the guard branch is never entered. [5](#0-4) [6](#0-5) [7](#0-6) 

The config comment itself acknowledges the risk: *"a huge batch request may cost a lot of memory or makes the RPC server slow"*, yet the protection is off by default.

**Exploit flow**

1. Attacker connects to the RPC endpoint (localhost by default; network-accessible if the operator changed `listen_address`).
2. Sends a single HTTP POST with a JSON array of ~33 000 minimal calls (e.g., `get_tip_block_number`) packed into the 2 MiB body.
3. The server parses the array, finds no batch limit, and processes every call sequentially via `stream::iter(calls).then(...)`.
4. Substituting computationally heavier calls (e.g., `dry_run_transaction` with a script that consumes the full cycle budget) amplifies CPU exhaustion per call.
5. The node's async runtime is saturated; P2P sync, block relay, and chain processing stall. [8](#0-7) 

### Impact Explanation

A caller with RPC access can cause sustained CPU and memory exhaustion on the node, making it unable to process new blocks, relay transactions, or respond to peers. This constitutes **service unavailability / severe degradation** under realistic attacker input. If the node falls behind the chain tip during the attack, it may miss time-sensitive operations (e.g., DAO withdrawal deadlines, Lightning channel force-closes). The `max_request_body_size` misconfiguration also means operators who believe they have restricted or enlarged the body limit are silently operating with axum's hardcoded 2 MiB default, violating their security assumptions.

### Likelihood Explanation

The RPC binds to `127.0.0.1` by default, so the baseline attacker is a local process or a user with shell access. This is explicitly listed as in-scope ("supported local CLI/RPC user"). Many production deployments also expose the RPC to internal networks or behind a reverse proxy, widening the attack surface. No authentication is required by default. The exploit requires only a single HTTP POST and no special knowledge beyond the JSON-RPC specification.

### Recommendation

1. **Apply the configured body limit**: In `RpcServer::start_server`, add `axum::extract::DefaultBodyLimit::max(config.max_request_body_size)` as a router layer so the operator-configured value is actually enforced.
2. **Enable batch limit by default**: Set a safe default for `rpc_batch_limit` (e.g., 100–200) in the default configuration and in `RpcConfig`, rather than leaving it opt-in.
3. **Enforce the limit unconditionally**: Consider making `JSONRPC_BATCH_LIMIT` always initialised (with a default value) rather than only when the config field is present.

### Proof of Concept

```python
import json, requests

# Build a batch of 30,000 minimal calls within the 2 MiB body limit
batch = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(30_000)]

# Single request, no authentication needed
r = requests.post("http://127.0.0.1:8114/", json=batch, timeout=300)
# Node processes all 30,000 calls sequentially; RPC becomes unresponsive
# during processing; P2P and chain services stall
print(len(r.json()), "responses received")
```

For amplified CPU exhaustion, replace `get_tip_block_number` with `dry_run_transaction` carrying a script that consumes the maximum allowed cycle count, multiplying the per-call cost by orders of magnitude within the same 2 MiB envelope. [6](#0-5) [9](#0-8)

### Citations

**File:** rpc/src/server.rs (L34-95)
```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();

#[doc(hidden)]
#[derive(Debug)]
pub struct RpcServer {
    pub http_address: SocketAddr,
    pub tcp_address: Option<SocketAddr>,
    pub ws_address: Option<SocketAddr>,
}

impl RpcServer {
    /// Creates an RPC server.
    ///
    /// ## Parameters
    ///
    /// * `config` - RPC config options.
    /// * `io_handler` - RPC methods handler. See [ServiceBuilder](../service_builder/struct.ServiceBuilder.html).
    /// * `handler` - Tokio runtime handle.
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }

        let rpc = Arc::new(io_handler);

        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();

        let ws_address = if let Some(addr) = config.ws_listen_address {
            let local_addr =
                Self::start_server(&rpc, addr, handler.clone(), true).inspect(|&addr| {
                    info!("Listen WebSocket RPCServer on address: {}", addr);
                });
            local_addr.ok()
        } else {
            None
        };

        let tcp_address = if let Some(addr) = config.tcp_listen_address {
            let local_addr = handler.block_on(Self::start_tcp_server(rpc, addr, handler.clone()));
            if let Ok(addr) = &local_addr {
                info!("Listen TCP RPCServer on address: {}", addr);
            };
            local_addr.ok()
        } else {
            None
        };

        Self {
            http_address,
            tcp_address,
            ws_address,
        }
    }
```

**File:** rpc/src/server.rs (L119-130)
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

**File:** rpc/src/server.rs (L218-298)
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
```

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
