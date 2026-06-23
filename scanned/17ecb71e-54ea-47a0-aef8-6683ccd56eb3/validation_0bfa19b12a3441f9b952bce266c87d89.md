### Title
Unbounded HTTP Request Body Collection in Miner Notify Listener Enables Memory Exhaustion DoS — (`miner/src/client.rs`)

---

### Summary

The CKB miner's optional block-template notify listener accepts inbound HTTP connections and reads the entire request body into memory with no size cap. Any network peer (or local process) that can reach the configured listen address can send an arbitrarily large body, causing unbounded heap allocation and an OOM crash of the miner process, halting block submission.

---

### Finding Description

When the miner is started in **notify mode** (`listen` is set in `ckb-miner.toml`), `Client::spawn_background` calls `listen_block_template_notify`, which binds a TCP listener and dispatches every accepted connection to the `handle` async function:

```rust
// miner/src/client.rs  lines 358-369
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();   // ← no size limit

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
}
```

`BodyExt::collect(req).await?` streams the full incoming body into a contiguous in-memory buffer before any parsing occurs. There is no `Content-Length` check, no `http_body_util::Limited` wrapper, and no per-connection byte cap anywhere in the call chain. [1](#0-0) 

The listener itself accepts connections from any source on the configured socket address: [2](#0-1) 

By contrast, the CKB **node's** JSON-RPC server enforces `max_request_body_size = 10485760` (10 MiB): [3](#0-2) 

No equivalent guard exists for the miner's HTTP listener.

The block-assembler side (`tx-pool/src/block_assembler/mod.rs`) sends legitimate POST requests to this URL, but the listener accepts connections from **any** source — it performs no authentication and no size enforcement: [4](#0-3) 

---

### Impact Explanation

An attacker who can reach the miner's listen port sends an HTTP POST with an arbitrarily large body (e.g., streaming gigabytes). The miner process allocates memory proportional to the body size before any parsing or rejection occurs. This causes:

1. **OOM crash** of the miner process — block submission stops entirely.
2. **Sustained DoS** — the attacker can reconnect and repeat, keeping the miner offline.
3. **Mining revenue loss** — valid blocks are never submitted while the miner is down.

The impact is analogous to the reference report's "operator reverts the call, causing the whole transaction to fail" — here, the attacker causes the miner process to crash, denying all block submissions.

---

### Likelihood Explanation

The `listen` feature is opt-in (commented out by default in `ckb-miner.toml`): [5](#0-4) 

However:
- Users who enable notify mode for performance reasons may bind to `0.0.0.0:PORT` or a public IP, making the port reachable from the internet.
- Even with the default `127.0.0.1` binding, any local process (e.g., a compromised co-located service) can exploit this.
- No authentication or IP allowlist is enforced by the code.
- The attack requires only a single TCP connection and standard HTTP tooling.

Likelihood: **Medium** (conditional on notify mode being enabled; high within that population).

---

### Recommendation

Wrap the incoming body with a size limit before collecting, mirroring the RPC server's `max_request_body_size`:

```rust
use http_body_util::Limited;

async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    // Reject bodies larger than, e.g., 10 MiB
    const MAX_BODY: u64 = 10 * 1024 * 1024;
    let limited = Limited::new(req, MAX_BODY);
    let body = match BodyExt::collect(limited).await {
        Ok(b) => b.aggregate(),
        Err(_) => return Ok(Response::new(Empty::new())), // silently drop oversized requests
    };

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
}
```

Additionally, consider adding an IP allowlist so only the configured CKB node address can POST to the listener.

---

### Proof of Concept

```bash
# Attacker sends an unbounded streaming body to the miner notify listener
# (replace <miner-host>:<port> with the configured listen address)
python3 -c "
import socket, time
s = socket.create_connection(('<miner-host>', 8888))
# Send HTTP headers indicating a huge body
s.sendall(b'POST / HTTP/1.1\r\nHost: miner\r\nContent-Type: application/json\r\nContent-Length: 10000000000\r\n\r\n')
# Stream garbage data; miner allocates memory for each chunk
while True:
    s.sendall(b'A' * 65536)
    time.sleep(0.01)
"
```

The miner process's RSS grows without bound until the OS OOM-killer terminates it or the system becomes unresponsive, halting all block submissions.

### Citations

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

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** tx-pool/src/block_assembler/mod.rs (L690-711)
```rust
            for url in &self.config.notify {
                if let Ok(req) = Request::builder()
                    .method(Method::POST)
                    .uri(url.as_ref())
                    .header("content-type", "application/json")
                    .body(Full::new(template_json.to_owned().into()))
                {
                    let client = Arc::clone(&self.poster);
                    let url = url.to_owned();
                    tokio::spawn(async move {
                        let _resp =
                            timeout(notify_timeout, client.request(req))
                                .await
                                .map_err(|_| {
                                    ckb_logger::warn!(
                                        "block assembler notifying {} timed out",
                                        url
                                    );
                                });
                    });
                }
            }
```

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```
