The relevant code is in `miner/src/client.rs`. Let me confirm the exact lines and check the config for the listen address defaults.

### Title
Unbounded HTTP Request Body Buffering in Miner Notify Endpoint Enables Remote OOM DoS — (`miner/src/client.rs`)

### Summary

The miner's HTTP notify listener (`handle()`) calls `BodyExt::collect(req).await?.aggregate()` with no body-size cap. Any TCP peer that can reach the configured `listen` address can send an arbitrarily large POST body, forcing the miner process to buffer it entirely in memory until the OS OOM-kills it.

### Finding Description

When `Client::spawn_background` detects a configured `listen` address, it starts `listen_block_template_notify`, which accepts raw TCP connections and dispatches each request to `handle()`. [1](#0-0) 

Inside `handle()`, the entire incoming body is collected into a contiguous in-memory buffer before any parsing occurs: [2](#0-1) 

There is no `Content-Length` check, no `take(limit)` wrapper, no 413 response, and no hyper-level `max_buf_size` configuration applied before this call. `BodyExt::collect` will stream and accumulate every byte the sender transmits.

The `listen` field is `Option<SocketAddr>` with no enforcement that it must be a loopback address: [3](#0-2) 

There is also no authentication layer on the endpoint — `handle()` accepts any HTTP request unconditionally.

### Impact Explanation

An attacker who can reach the listen socket (e.g., operator configured `listen = "0.0.0.0:PORT"` for a remote-miner setup, or via SSRF on the same host) sends a single HTTP POST with a multi-gigabyte body. The miner's Tokio runtime buffers the entire body, exhausting available RAM. The OS OOM-killer terminates the miner process, halting block production until an operator manually restarts it.

### Likelihood Explanation

The notify mode is a documented, supported production configuration explicitly described in the startup log message: [4](#0-3) 

Operators who run the CKB node and miner on separate machines must bind `listen` to a non-loopback address, making the endpoint network-reachable. The attack requires only a single TCP connection and a streaming HTTP POST — no credentials, no PoW, no prior state.

### Recommendation

Apply a body-size limit before collecting. For example, wrap the incoming body with a `http_body_util::Limited` adapter (or equivalent `take` on the stream) before calling `collect`:

```rust
// Reject bodies larger than, e.g., 4 MB
let limited = req.into_body().take(4 * 1024 * 1024);
let body = BodyExt::collect(limited).await?.aggregate();
```

Return HTTP 413 if the limit is exceeded. Additionally, consider requiring a shared secret token in a request header so that only the paired CKB node can post to this endpoint.

### Proof of Concept

```bash
# Start miner with listen = "0.0.0.0:18114"
# Then from any reachable host:
python3 -c "
import socket, time
s = socket.create_connection(('MINER_IP', 18114))
# Send chunked HTTP POST with 2 GB body
header = (
    'POST / HTTP/1.1\r\n'
    'Host: MINER_IP\r\n'
    'Content-Type: application/json\r\n'
    'Content-Length: 2147483648\r\n'
    '\r\n'
).encode()
s.sendall(header)
chunk = b'A' * (1024 * 1024)  # 1 MB chunks
for _ in range(2048):          # 2 GB total
    s.sendall(chunk)
s.close()
"
# Expected (vulnerable): miner process RSS grows to available RAM, then is OOM-killed.
# Expected (fixed): server responds 413 after exceeding the configured limit.
```

### Citations

**File:** miner/src/client.rs (L208-221)
```rust
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

**File:** miner/src/client.rs (L358-368)
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
```

**File:** util/app-config/src/configs/miner.rs (L28-30)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```
