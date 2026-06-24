All four cited code references are confirmed exactly as described in the report. Let me verify the complete picture:

All cited code is confirmed exactly as described. No size-limiting guard (`Limited`, `max_body`, `body_limit`, or content-length check) exists anywhere in `miner/src/`.

---

Audit Report

## Title
Unbounded HTTP Request Body in Miner Notify Listener Enables Remote Memory Exhaustion Crash — (`miner/src/client.rs`)

## Summary
When the miner's optional notify listen mode is enabled, the `handle` async function collects the full incoming HTTP request body into memory with no size cap. A remote attacker who can reach the configured listen address can stream an arbitrarily large body, causing unbounded heap growth and an OOM crash of the miner process, halting block submission for that operator.

## Finding Description
`Client::spawn_background` conditionally calls `listen_block_template_notify` when `self.config.listen` is `Some`, which binds a `TcpListener` and dispatches every accepted connection to `handle`: [1](#0-0) 

The `handle` function collects the entire body unconditionally: [2](#0-1) 

`BodyExt::collect` streams the entire body into memory before any parsing. There is no `Content-Length` check, no `http_body_util::Limited` wrapper, and no per-connection byte cap anywhere in the call chain — confirmed by grep across all of `miner/src/`. The listener accepts connections from any source on the configured socket address with no authentication. [3](#0-2) 

By contrast, the CKB node's JSON-RPC server enforces `max_request_body_size = 10485760` (10 MiB): [4](#0-3) 

No equivalent guard exists for the miner's HTTP listener.

## Impact Explanation
Crashing the miner process halts block submission for the affected operator. The miner is a separate CLI process from the CKB node; the node itself and the network are unaffected. This matches the allowed bounty impact: **Note (0–500 points): Any local command line crash**. The crash is remotely triggerable when the listen address is reachable, but the blast radius is limited to a single miner operator's process.

## Likelihood Explanation
The `listen` feature is opt-in and commented out by default: [5](#0-4) 

When enabled, the default binding is `127.0.0.1:8888`, restricting access to local processes. Users who bind to `0.0.0.0` or a public IP expose the port to the internet. Even with localhost binding, any local process can exploit this. The attack requires only a single TCP connection and standard HTTP tooling, and is trivially repeatable.

## Recommendation
Wrap the incoming body with `http_body_util::Limited` before collecting, mirroring the RPC server's `max_request_body_size`:

```rust
use http_body_util::Limited;

const MAX_BODY: u64 = 10 * 1024 * 1024;
let limited = Limited::new(req, MAX_BODY);
let body = match BodyExt::collect(limited).await {
    Ok(b) => b.aggregate(),
    Err(_) => return Ok(Response::new(Empty::new())),
};
```

Additionally, consider restricting accepted connections to the configured CKB node's IP address only.

## Proof of Concept
```bash
python3 -c "
import socket, time
s = socket.create_connection(('127.0.0.1', 8888))
s.sendall(b'POST / HTTP/1.1\r\nHost: miner\r\nContent-Type: application/json\r\nContent-Length: 10000000000\r\n\r\n')
while True:
    s.sendall(b'A' * 65536)
    time.sleep(0.01)
"
```
Monitor the miner process RSS with `watch -n1 'ps -o rss= -p $(pgrep ckb-miner)'`; it grows without bound until the OS OOM-killer terminates the process.

### Citations

**File:** miner/src/client.rs (L204-224)
```rust
    pub fn spawn_background(self) {
        let client = self.clone();
        if let Some(addr) = self.config.listen {
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
            self.handle.spawn(async move {
                client.listen_block_template_notify(addr).await;
            });
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

**File:** miner/src/client.rs (L358-362)
```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```
