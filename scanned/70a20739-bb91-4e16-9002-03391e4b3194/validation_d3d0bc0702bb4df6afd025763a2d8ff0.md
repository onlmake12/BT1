The code confirms the vulnerability. The `handle` function at line 362 of `miner/src/client.rs` calls `BodyExt::collect(req).await?.aggregate()` with no body size limit, and the grep confirms there is zero body-size enforcement anywhere in the miner codebase.

---

### Title
Unbounded HTTP body allocation in miner notify handler causes OOM — (`miner/src/client.rs`)

### Summary
The miner's notify-mode HTTP server collects the full incoming request body into memory with no size cap. Any process that can reach the listen address can send an arbitrarily large body and exhaust the miner process's heap.

### Finding Description
When the miner is started with `listen = Some(addr)`, `listen_block_template_notify` binds a TCP listener and dispatches every accepted connection to the `handle` async function. [1](#0-0) 

Inside `handle`, the entire request body is collected unconditionally: [2](#0-1) 

Neither the `auto::Builder` setup nor any middleware applies a body-size limit: [3](#0-2) 

A grep for `body_limit`, `max_body`, `content_length`, or `size_limit` in the entire `miner/` tree returns zero matches, confirming there is no guard anywhere in the call path.

### Impact Explanation
An attacker who can open a TCP connection to the miner's notify port (typically `127.0.0.1:<port>`, reachable by any local process without privilege) can POST a multi-gigabyte body. `BodyExt::collect` streams all chunks into a contiguous `Bytes` aggregate, growing the heap until the OS OOM-killer terminates the miner process. This halts all block submission for the duration of the outage.

### Likelihood Explanation
The notify listen address is a documented, supported production feature. On a shared host or any machine where an unprivileged user can open local TCP connections (the default on Linux/macOS), the precondition is trivially met. No credentials, keys, or elevated privileges are required.

### Recommendation
Apply a body-size limit before collecting. With hyper-util's `auto::Builder` the idiomatic fix is to wrap the incoming body with `http_body_util::Limited`:

```rust
use http_body_util::Limited;

const MAX_BODY: u64 = 4 * 1024 * 1024; // 4 MiB — far larger than any real block template

async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let limited = Limited::new(req, MAX_BODY);
    let body = BodyExt::collect(limited).await?.aggregate();
    // ...
}
```

Alternatively, check `Content-Length` before collecting and reject oversized requests immediately.

### Proof of Concept
```bash
# Start ckb-miner with listen = "127.0.0.1:18114" in miner.toml
# Then from any local shell (no privileges needed):
python3 -c "
import socket, time
s = socket.create_connection(('127.0.0.1', 18114))
# Send a chunked HTTP/1.1 POST with a 4 GB body
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

### Citations

**File:** miner/src/client.rs (L236-237)
```rust
        let server = auto::Builder::new(TokioExecutor::new());
        let graceful = GracefulShutdown::new();
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
