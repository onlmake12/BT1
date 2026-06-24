Audit Report

## Title
Unbounded HTTP request body buffering in miner notify server leads to OOM crash — (`miner/src/client.rs`)

## Summary
The `handle` function in `miner/src/client.rs` collects the full incoming HTTP request body into memory with no size limit. Any TCP client that can reach the configured notify listen address can exhaust process memory and crash the miner process by streaming an arbitrarily large body.

## Finding Description
At line 362 of `miner/src/client.rs`, `handle()` calls `BodyExt::collect(req).await?.aggregate()` directly on the raw `hyper::body::Incoming` body with no size cap applied before or during collection. No `http_body_util::Limited` wrapper, no manual byte-count guard, and no frame-count limit is present. The `hyper::body::Incoming` type carries no application-level size restriction of its own. The notify listener is spawned unconditionally whenever `config.listen` is `Some(addr)` (line 206), which is a documented, supported production feature — the log message at lines 207–221 explicitly instructs operators to configure it. Each accepted connection is handled in its own `tokio::spawn`ed task (lines 257–261), meaning multiple concurrent oversized requests can be in-flight simultaneously, compounding memory pressure.

## Impact Explanation
An attacker sends an HTTP POST with a chunked transfer-encoded body of arbitrary size. The Tokio task servicing that connection allocates heap memory proportional to the body size before any application logic runs. Sending sufficient data triggers the Linux OOM killer or a Rust allocator panic, terminating the miner process. This constitutes a crash of the `ckb-miner` command-line tool, matching the allowed CKB bounty impact: **"Any local command line crash" (Note, 0–500 points)**.

## Likelihood Explanation
The notify listen address defaults to loopback (`127.0.0.1:PORT`) in typical deployments. Any unprivileged local user account on the same host can open a TCP connection to a loopback port without elevated privileges, no authentication, no token, and no special capability. If the operator binds to `0.0.0.0`, any LAN or internet peer qualifies. The attack requires a single HTTP request and is trivially repeatable.

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

`Limited` returns `Err` (mapped to `LengthLimitError`) once the threshold is exceeded, aborting the connection without buffering the full payload.

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
```