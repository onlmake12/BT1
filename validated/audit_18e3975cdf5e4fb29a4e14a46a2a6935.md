### Title
Unauthenticated Miner Notify Endpoint Allows Any HTTP Caller to Redirect Mining Rewards — (`miner/src/client.rs`)

### Summary

The CKB miner client supports a "notify mode" where it listens on a configurable TCP address for incoming `BlockTemplate` HTTP pushes from the node. The HTTP handler that receives these pushes performs **no authentication whatsoever**: any HTTP client that can reach the listen address can inject an arbitrary `BlockTemplate`, including a crafted coinbase transaction whose output lock script points to the attacker's address. The miner will immediately begin mining with the injected template, and any block it finds will pay all block rewards to the attacker.

### Finding Description

When the miner is started with `listen` mode enabled (e.g., `listen = "0.0.0.0:8888"` in `ckb-miner.toml`), `Client::spawn_background` calls `listen_block_template_notify`: [1](#0-0) 

`listen_block_template_notify` binds a `TcpListener` to the configured address and serves every incoming connection through the `handle` function: [2](#0-1) 

The `handle` function is:

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
``` [3](#0-2) 

There is **no IP allowlist check, no shared secret, no HMAC, no token, no TLS client certificate** — nothing. Any HTTP POST that deserializes as a valid `BlockTemplate` is accepted unconditionally.

`update_block_template` immediately forwards the injected template to the mining workers: [4](#0-3) 

A `BlockTemplate` carries a `cellbase` field — the coinbase transaction — whose output lock script determines who receives the block reward: [5](#0-4) 

The attacker crafts a `BlockTemplate` with `cellbase.data.outputs[0].lock` set to their own lock script. The miner mines with this template and, upon finding a valid PoW solution, submits the block via `submit_block`. The CKB consensus rules place no restriction on what lock script a coinbase output may use, so the block is accepted and the full block reward is paid to the attacker's address.

The `listen` field is a first-class, documented, supported configuration option: [6](#0-5) 

The default template comments it out at `127.0.0.1:8888`, but the code accepts any `SocketAddr`, including `0.0.0.0:8888`. [7](#0-6) 

### Impact Explanation

Any attacker who can send a single HTTP POST to the miner's notify port can silently redirect **all future block rewards** to an address they control. The miner continues operating normally (PoW is still solved, blocks are still submitted) but every coinbase output goes to the attacker. The legitimate miner operator receives nothing. This is a direct, complete theft of mining revenue with no recovery path for already-mined blocks.

### Likelihood Explanation

The attack is realistic in two concrete scenarios:

1. **Network-reachable endpoint**: A miner who binds to `0.0.0.0:8888` (common in pool/farm setups where the node and miner run on different hosts) exposes the endpoint to any host on the same network segment or, if firewalled incorrectly, to the internet. No credentials are needed.
2. **Local co-tenant**: On a shared host or cloud VM, any co-located process (another user, a compromised dependency, a container escape) can POST to `127.0.0.1:8888` without any privileges.

The feature is explicitly documented and actively used in production mining setups. The attacker needs only the ability to send one HTTP request.

### Recommendation

Add authentication to the notify endpoint. The simplest correct fix is a shared secret (bearer token or HMAC-SHA256 over the body) configured in `ckb-miner.toml` alongside the `listen` address. The `handle` function must reject any request that does not present the correct credential before deserializing or acting on the body. Alternatively, restrict accepted connections to a configurable IP allowlist checked immediately after `listener.accept()`.

### Proof of Concept

1. Configure `ckb-miner.toml` with `listen = "0.0.0.0:8888"` and start the miner.
2. From any reachable host, obtain a valid `BlockTemplate` JSON from the node's `get_block_template` RPC.
3. Replace `cellbase.data.outputs[0].lock` with an attacker-controlled lock script (e.g., secp256k1 with the attacker's pubkey hash).
4. POST the modified template:
   ```
   curl -X POST http://<miner-ip>:8888/ \
        -H 'Content-Type: application/json' \
        -d '<modified_block_template_json>'
   ```
5. The miner's `current_work_id` updates and workers immediately begin mining with the injected cellbase. The next block found pays all rewards to the attacker's address.

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

**File:** util/jsonrpc-types/src/block_template.rs (L74-76)
```rust
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
```

**File:** util/app-config/src/configs/miner.rs (L28-29)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
```

**File:** resource/ckb-miner.toml (L59-60)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"
```
