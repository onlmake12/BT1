### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Endpoint — (`miner/src/client.rs`)

### Summary

The CKB miner's "notify mode" HTTP listener accepts block template pushes from any source without authentication or IP restriction. Any attacker who can reach the listening socket can inject an arbitrary `BlockTemplate`, redirecting the miner's hashpower to work on attacker-controlled blocks — including blocks with attacker-controlled coinbase outputs.

### Finding Description

When the miner is configured with a `listen` address (notify mode), `listen_block_template_notify` binds a TCP socket and serves an HTTP endpoint. Every incoming connection is dispatched to the `handle` function: [1](#0-0) 

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

There is no check on:
- The source IP address of the connection
- Any shared secret / token in the request headers
- Whether the `BlockTemplate` was actually issued by the local CKB node

`update_block_template` immediately replaces the active work: [2](#0-1) 

The listener itself also performs no IP filtering before accepting connections: [3](#0-2) 

This is structurally identical to the tbtc `approvedToLog` stub that returned `true` for every caller — the CKB equivalent is an HTTP handler that trusts every caller unconditionally.

### Impact Explanation

An attacker who can reach the miner's notify port (e.g., on a misconfigured or publicly exposed host, or from within the same LAN/VPN) can:

1. **Redirect coinbase rewards** — push a `BlockTemplate` whose `cellbase` transaction pays the attacker's lock script. The miner solves PoW and submits a block that pays the attacker.
2. **Waste hashpower** — push templates referencing a stale or invalid parent, causing the miner to produce orphan blocks or blocks that will be rejected.
3. **Suppress valid blocks** — continuously push new templates, resetting the miner's work ID and preventing it from ever finishing a valid solution.

### Likelihood Explanation

- Notify mode is a documented, supported configuration (`resource/ckb-miner.toml` and `util/app-config/src/configs/miner.rs` expose the `listen` field).
- Mining operators commonly bind the notify port to `0.0.0.0` for convenience or to allow the CKB node on a different host to push templates.
- No firewall rule or OS-level restriction is enforced by the code itself; the attack surface is entirely determined by network reachability.

### Recommendation

1. **Bind to loopback by default** — change the default `listen` address to `127.0.0.1` so the port is not reachable from the network without explicit operator action.
2. **Shared-secret token** — require a configurable bearer token in the `Authorization` header; reject requests that omit or mismatch it (analogous to how the tbtc fix restricted log calls to known `TBTCDepositToken` holders).
3. **Source-IP allowlist** — optionally, allow operators to configure a list of permitted source IPs (e.g., only the local CKB node's address).

### Proof of Concept

```
# Miner is running in notify mode, listening on 0.0.0.0:18114
# Attacker crafts a BlockTemplate with attacker-controlled cellbase

curl -X POST http://<miner-host>:18114/ \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1a08a97e",
    "current_time": "0x...",
    "number": "0x...",
    "epoch": "0x...",
    "parent_hash": "0x<valid tip hash>",
    "cycles_limit": "0x...",
    "bytes_limit": "0x...",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": { ... <attacker coinbase paying attacker lock> ... }
    },
    "work_id": "0x1",
    "dao": "0x..."
  }'
```

The `handle` function parses this payload and calls `client.update_block_template(template)` with no validation of origin. The miner immediately begins solving PoW for the attacker-supplied template. [4](#0-3)

### Citations

**File:** miner/src/client.rs (L234-270)
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
```

**File:** miner/src/client.rs (L293-312)
```rust
    fn update_block_template(&self, block_template: BlockTemplate) {
        let work_id = block_template.work_id.into();
        let updated = |id| {
            if id != work_id || id == 0 {
                Some(work_id)
            } else {
                None
            }
        };
        if self
            .current_work_id
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
            .is_ok()
        {
            let work: Work = block_template.into();
            if let Err(e) = self.new_work_tx.send(Works::New(work)) {
                error!("notify_new_block error: {:?}", e);
            }
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
