Audit Report

## Title
Unbounded HTTP Body Buffering in Miner Notify Listener Enables OOM via Unauthenticated Request — (File: miner/src/client.rs)

## Summary
The miner's block-template notify HTTP listener accepts inbound TCP connections and dispatches each to the `handle` function, which calls `BodyExt::collect(req).await?.aggregate()` with no body-size limit. An attacker who can reach the listen socket can stream an arbitrarily large HTTP body, causing the miner process to exhaust host memory and be OOM-killed, halting block submissions until the miner is restarted.

## Finding Description
When `config.listen` is `Some(addr)`, `Client::spawn_background` spawns `listen_block_template_notify` at [1](#0-0) , which binds a raw `TcpListener` and serves each accepted connection through hyper's `auto::Builder` with no per-connection body limit. Every accepted connection is dispatched to the module-level `handle` function: [2](#0-1) 

`BodyExt::collect` at line 362 reads all body frames from the incoming stream and accumulates them into a single in-memory buffer before `serde_json::from_reader` is called. [3](#0-2) 

A grep across all of `miner/src/` for any body-size guard (`Limited`, `max_request_body_size`, `body.*limit`) returns zero matches. This contrasts with the CKB node's own RPC server, which enforces `max_request_body_size` configured in `resource/ckb.toml` and declared in `util/app-config/src/configs/rpc.rs`. [4](#0-3) 

## Impact Explanation
The concrete impact is an OOM-induced crash of the miner process. The miner is a standalone binary separate from the CKB node; crashing it does not crash the CKB node, does not cause consensus deviation, and does not affect the broader CKB network. This maps to **Note (0–500 points): Any local command line crash**.

## Likelihood Explanation
The notify listener is only active when the operator explicitly sets `config.listen` to `Some(addr)`. [5](#0-4)  If bound to `0.0.0.0:PORT`, any network-reachable host can exploit this with a single TCP connection and a streaming HTTP body. No authentication, no PoW, and no privileged access are required once the socket is reachable. The attack is repeatable on each miner restart.

## Recommendation
Wrap the incoming body with `http_body_util::Limited` before collecting, mirroring the guard used by the CKB node's RPC server:

```rust
use http_body_util::Limited;

const MAX_BODY: u64 = 10 * 1024 * 1024; // 10 MiB
let body = Limited::new(req, MAX_BODY)
    .collect()
    .await
    .map_err(|_| "body too large")?
    .aggregate();
```

Additionally, consider defaulting the notify listener bind address to `127.0.0.1` and documenting that exposing it to untrusted networks is unsafe.

## Proof of Concept
```python
import socket

HOST = "127.0.0.1"
PORT = 8119  # miner notify listen port

s = socket.create_connection((HOST, PORT))
header = (
    "POST / HTTP/1.1\r\n"
    f"Host: {HOST}:{PORT}\r\n"
    "Content-Type: application/json\r\n"
    "Content-Length: 1073741824\r\n"  # 1 GiB
    "\r\n"
).encode()
s.sendall(header)
chunk = b"A" * 65536
sent = 0
while sent < 1073741824:
    s.sendall(chunk)
    sent += len(chunk)
# Miner RSS grows to ~1 GB, then process is OOM-killed.
```

The miner process buffers the entire body in `BodyExt::collect` at `miner/src/client.rs:362` before attempting JSON deserialization, growing RSS proportionally until the host OOM-killer terminates it. [6](#0-5)

### Citations

**File:** miner/src/client.rs (L204-232)
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
            self.blocking_fetch_block_template();
        } else {
            ckb_logger::info!("loop poll mode: interval {}ms", self.config.poll_interval);
            self.handle.spawn(async move {
                client.poll_block_template().await;
            });
        }
    }
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

**File:** util/app-config/src/configs/rpc.rs (L1-50)
```rust
use ckb_jsonrpc_types::Script;
use serde::{Deserialize, Serialize};

/// RPC modules.
#[derive(Clone, Debug, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[allow(missing_docs)]
pub enum Module {
    Net,
    Chain,
    Miner,
    Pool,
    Experiment,
    Stats,
    IntegrationTest,
    Alert,
    Subscription,
    Debug,
    Indexer,
    RichIndexer,
    Terminal,
}

/// RPC config options.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
```
