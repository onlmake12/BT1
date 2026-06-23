### Title
Unauthenticated HTTP Notify Endpoint Allows Any Caller to Inject Arbitrary Block Templates into the Miner - (File: miner/src/client.rs)

### Summary
The CKB miner's optional "notify mode" binds an HTTP server that accepts `BlockTemplate` updates from any TCP client without authentication or source verification. Any attacker who can reach the miner's listen port can POST a crafted `BlockTemplate` JSON payload, causing the miner to immediately switch to mining on attacker-controlled work. This enables mining reward theft (by substituting the coinbase address) or sustained hashpower waste (by injecting stale or invalid templates).

### Finding Description

When the miner is configured with `listen = "<addr>"` in `ckb-miner.toml`, `Client::spawn_background` calls `listen_block_template_notify(addr)`, which binds a raw TCP listener and dispatches every incoming HTTP request to the `handle()` function:

```rust
// miner/src/client.rs lines 358-369
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
``` [1](#0-0) 

There is **no authentication check, no IP allowlist, no HMAC/token verification** of any kind. The `handle()` function accepts any well-formed `BlockTemplate` JSON from any TCP peer and immediately calls `update_block_template()`.

`update_block_template()` accepts the injected template whenever its `work_id` differs from the current one (or is zero), then sends `Works::New(work)` to the miner workers:

```rust
// miner/src/client.rs lines 293-312
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
        if let Err(e) = self.new_work_tx.send(Works::New(work)) { ... }
    }
}
``` [2](#0-1) 

The listener itself performs no peer filtering:

```rust
// miner/src/client.rs lines 234-271
async fn listen_block_template_notify(&self, addr: SocketAddr) {
    let listener = TcpListener::bind(addr).await.unwrap();
    ...
    loop {
        let client = self.clone();
        let handle = service_fn(move |req| handle(client.clone(), req));
        tokio::select! {
            conn = listener.accept() => {
                let (stream, _) = match conn { Ok(conn) => conn, ... };
                ...
            }
        }
    }
}
``` [3](#0-2) 

The notify mode is a documented, supported feature. The configuration file explicitly shows how to enable it:

```toml
# enable listen notify mode
# listen = "127.0.0.1:8888"
``` [4](#0-3) 

Operators running remote miners (a common production setup where the PoW worker is on a separate machine from the CKB node) must bind to a non-loopback address such as `0.0.0.0:8888`. The `listen` field is a plain `SocketAddr` with no restriction: [5](#0-4) 

The block assembler on the CKB node side POSTs the template to the configured `notify` URLs: [6](#0-5) 

There is no shared secret, HMAC, or token exchanged between the block assembler and the miner's HTTP server. Any third party that can reach the port is indistinguishable from the legitimate CKB node.

### Impact Explanation

An attacker who can reach the miner's HTTP notify port can:

1. **Mining reward theft**: Inject a `BlockTemplate` with a different `cellbase` transaction that pays the block reward to the attacker's address. The miner will solve PoW for the attacker's block and submit it via `submit_block`. The CKB node will accept the block (it is otherwise valid), and the reward goes to the attacker.

2. **Hashpower denial**: Continuously inject templates with a stale `parent_hash` or invalid transactions. The miner wastes all hashpower on work that will be rejected by `submit_block`. The `work_id` bypass condition (`id != work_id || id == 0`) means the attacker only needs to rotate `work_id` values to keep overwriting the current work.

3. **Sustained mining disruption**: Because the miner's `blocking_fetch_block_template()` is called only once at startup in notify mode, and subsequent updates come exclusively from the HTTP endpoint, a persistent attacker can prevent the miner from ever mining a legitimate block.

### Likelihood Explanation

The notify mode is a first-class supported feature documented in `ckb-miner.toml` and the miner log output explicitly instructs operators to configure `notify = ["http://<addr>"]` in the block assembler. Any operator running a remote miner (PoW hardware on a different host than the CKB node) must expose this port on a non-loopback address. An attacker on the same network segment, or any attacker if the port is internet-exposed, can exploit this with a single HTTP POST containing a crafted JSON body.

### Recommendation

Add source authentication to the miner's HTTP notify endpoint. Options include:

1. **Shared secret / Bearer token**: Require a configurable `Authorization` header token in `ClientConfig`. The block assembler's notify POST should include the same token. Reject requests missing or presenting the wrong token.
2. **IP allowlist**: Add an optional `allowed_notify_ips` field to `ClientConfig` and reject connections from addresses not on the list inside `listen_block_template_notify`.
3. **Localhost-only enforcement**: Warn or refuse to start if `listen` is set to a non-loopback address without an explicit `allow_remote_notify = true` flag, reducing accidental exposure.

### Proof of Concept

Attacker steps (miner configured with `listen = "0.0.0.0:8888"`):

```bash
# Craft a BlockTemplate with attacker's coinbase address substituted
# work_id must differ from the current one (e.g., use 0x9999)
curl -X POST http://<miner-host>:8888/ \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1a08a97e",
    "current_time": "0x...",
    "number": "0x...",
    "epoch": "0x...",
    "parent_hash": "0x...",
    "cycles_limit": "0x...",
    "bytes_limit": "0x...",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": { <ATTACKER_COINBASE_TX_WITH_ATTACKER_LOCK_SCRIPT> }
    },
    "work_id": "0x9999",
    "dao": "0x..."
  }'
```

The miner's `handle()` deserializes this, `update_block_template()` accepts it (work_id `0x9999` ≠ current), and `Works::New(work)` is dispatched to all worker threads. The miner begins solving PoW for the attacker's block. Upon success, `submit_block` is called with the attacker's coinbase, and the CKB node mints the reward to the attacker's address.

### Citations

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

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```

**File:** util/app-config/src/configs/miner.rs (L28-30)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```

**File:** tx-pool/src/block_assembler/mod.rs (L683-712)
```rust
    pub(crate) async fn notify(&self) {
        if !self.need_to_notify() {
            return;
        }
        let template = self.get_current().await;
        if let Ok(template_json) = serde_json::to_string(&template) {
            let notify_timeout = Duration::from_millis(self.config.notify_timeout_millis);
            for url in &self.config.notify {
                if let Ok(req) = Request::builder()
                    .method(Method::POST)
                    .uri(url.as_ref())
                    .header("content-type", "application/json")
                    .body(Full::new(template_json.to_owned().into()))
                {
                    let client = Arc::clone(&self.poster);
                    let url = url.to_owned();
                    tokio::spawn(async move {
                        let _resp =
                            timeout(notify_timeout, client.request(req))
                                .await
                                .map_err(|_| {
                                    ckb_logger::warn!(
                                        "block assembler notifying {} timed out",
                                        url
                                    );
                                });
                    });
                }
            }

```
