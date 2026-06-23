The code is fully readable. Let me analyze the exact path.

The code is fully confirmed. Here is my analysis.

---

### Title
Unbounded HTTP Request Body Collection in Notify-Mode Miner Endpoint Enables Remote Memory Exhaustion (OOM) — (`miner/src/client.rs`)

---

### Summary

The miner's HTTP notify endpoint, active when `listen` is configured, collects the full incoming request body into process memory with no size limit. Any peer that can reach the socket can send arbitrarily large bodies in a loop, exhausting the miner process's RSS and triggering OOM.

---

### Finding Description

When the miner runs in notify mode, `listen_block_template_notify` binds a raw TCP listener and dispatches every accepted connection to the `handle` function via `service_fn`: [1](#0-0) 

The `handle` function unconditionally collects the entire incoming body before doing anything else: [2](#0-1) 

Specifically, line 362:

```rust
let body = BodyExt::collect(req).await?.aggregate();
```

There is:
- **No `Content-Length` check** before collection
- **No streaming size cap** (e.g., `http_body_util::Limited`)
- **No per-connection or per-request byte budget**
- **No authentication** on the endpoint

`hyper::body::Incoming` has no built-in body size limit; `BodyExt::collect` buffers every chunk until the sender closes the body. A sender that streams a multi-GB body will cause the miner to allocate that many bytes before `serde_json::from_reader` is even called. On parse failure the memory is released, but a second connection can immediately begin the same allocation — and with concurrent connections (each spawned with `tokio::spawn`) the allocations overlap. [3](#0-2) 

---

### Impact Explanation

The miner process is killed by the OS OOM killer (or panics on allocation failure). Mining stops entirely: no new block templates are processed, no nonces are submitted, and the operator suffers a complete mining outage until the process is restarted. Because the miner is a separate process from the CKB full node, the full node itself is unaffected, but the mining operation is fully disrupted.

---

### Likelihood Explanation

The precondition is that `config.listen` is set to a `SocketAddr` reachable from the network (e.g., `0.0.0.0:PORT` or a LAN/public IP). This is the intended configuration for distributed setups where the CKB node and miner run on separate hosts. No credentials, keys, or privileged access are required — the endpoint has no authentication. The attack is a simple HTTP POST loop and requires no protocol knowledge beyond TCP/HTTP.

---

### Recommendation

Wrap the incoming body with `http_body_util::Limited` before collecting it, enforcing a reasonable maximum (e.g., 4 MB, well above any legitimate block template):

```rust
use http_body_util::Limited;

async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    const MAX_BODY: u64 = 4 * 1024 * 1024; // 4 MB
    let limited = Limited::new(req, MAX_BODY);
    let body = BodyExt::collect(limited).await?.aggregate();
    // ...
}
```

This causes `collect` to return an error (and drop the connection) as soon as the body exceeds the cap, before any significant allocation occurs.

---

### Proof of Concept

```bash
# Start miner with listen = "0.0.0.0:18114" in config

# Send 50 concurrent 200 MB bodies
for i in $(seq 1 50); do
  dd if=/dev/urandom bs=1M count=200 | \
    curl -s -X POST http://<miner-ip>:18114/ \
         -H "Content-Type: application/json" \
         --data-binary @- &
done
wait

# Observe: miner process RSS grows to several GB and is killed by OOM killer
# or exits with an allocation error.
```

Each connection is handled in its own `tokio::spawn` task [4](#0-3) , so all 50 allocations proceed concurrently, multiplying the memory pressure.

### Citations

**File:** miner/src/client.rs (L234-242)
```rust
    async fn listen_block_template_notify(&self, addr: SocketAddr) {
        let listener = TcpListener::bind(addr).await.unwrap();
        let server = auto::Builder::new(TokioExecutor::new());
        let graceful = GracefulShutdown::new();
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        loop {
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
```

**File:** miner/src/client.rs (L254-261)
```rust
                    let conn = server.serve_connection_with_upgrades(stream, handle);

                    let conn = graceful.watch(conn.into_owned());
                    tokio::spawn(async move {
                        if let Err(err) = conn.await {
                            info!("connection error: {}", err);
                        }
                    });
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
