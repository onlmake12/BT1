### Title
Unbounded HTTP Body Buffering in Miner Notify Handler Allows Local OOM Crash — (`miner/src/client.rs`)

### Summary

The miner's HTTP notify server unconditionally buffers the entire request body into memory with no size limit. A local attacker can send a single HTTP POST with an arbitrarily large body to exhaust process memory and crash the miner.

### Finding Description

The `handle` function serving the miner's block-template notify endpoint calls `BodyExt::collect(req).await?.aggregate()` with no preceding body-size check: [1](#0-0) 

`BodyExt::collect` from `http_body_util` accumulates every incoming chunk into a `Collected<Bytes>` held entirely in heap memory. No `Content-Length` guard, no streaming limit, and no read timeout are applied before or after this call.

The server is started unconditionally whenever `config.listen` is set: [2](#0-1) 

The `hyper_util` `auto::Builder` used here adds no default body-size cap. Each accepted connection is spawned as an independent Tokio task, so multiple concurrent oversized requests can be sent simultaneously.

### Impact Explanation

An attacker with local TCP access to the notify port (typically `127.0.0.1:<port>`) sends a single HTTP POST with a multi-gigabyte chunked body. The miner process allocates memory proportional to the body size until the OS OOM-killer terminates it or the process panics on allocation failure. This crashes the miner, halting block submission and causing the operator to miss block rewards. Impact is scoped to the miner process (local RPC API crash), not the full CKB node.

### Likelihood Explanation

Any unprivileged process on the same host can open a TCP connection to localhost. No authentication is required on the notify endpoint. The attack requires a single HTTP request and is trivially scriptable with `curl --limit-rate 0 -d @/dev/zero`.

### Recommendation

Apply a body-size limit before collecting. With `http_body_util`, wrap the incoming body with `Limited`:

```rust
use http_body_util::Limited;

const MAX_BODY: u64 = 4 * 1024 * 1024; // 4 MiB, well above any real block template

async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let limited = Limited::new(req.into_body(), MAX_BODY);
    let body = BodyExt::collect(limited).await?.aggregate();
    // ...
}
```

Alternatively, reject requests whose `Content-Length` header exceeds the limit before reading the body at all.

### Proof of Concept

```bash
# Start miner with listen = "127.0.0.1:18114" in config
# Then from any local shell:
python3 -c "
import socket, time
s = socket.create_connection(('127.0.0.1', 18114))
# Send chunked HTTP/1.1 request with no Content-Length
s.sendall(b'POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n')
chunk = b'A' * 65536
hex_len = hex(len(chunk))[2:].encode() + b'\r\n'
while True:
    s.sendall(hex_len + chunk + b'\r\n')
    time.sleep(0.001)
"
# Monitor: watch -n1 'ps -o rss= -p $(pgrep ckb-miner)'
# RSS grows unboundedly until OOM kill
``` [3](#0-2)

### Citations

**File:** miner/src/client.rs (L234-254)
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
