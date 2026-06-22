### Title
Unauthenticated Miner Notify HTTP Endpoint Allows Any Peer to Inject Fake Block Templates - (`miner/src/client.rs`)

---

### Summary

When the CKB miner is configured in "notify mode" (`listen` address set in `ckb-miner.toml`), it starts an HTTP server that accepts block template push notifications. The `handle` function that processes incoming HTTP requests performs **no authentication or source verification** before calling `client.update_block_template(template)`. Any network-reachable attacker can POST a crafted `BlockTemplate` JSON payload to this endpoint, causing the miner to immediately abandon its legitimate work and begin mining on an attacker-controlled template, wasting hashpower and halting valid block submission.

---

### Finding Description

The CKB miner supports two operating modes: poll mode (periodic RPC calls to `get_block_template`) and notify mode (the CKB node pushes new templates via HTTP POST to the miner's listen address). When notify mode is enabled, `Client::spawn_background` calls `listen_block_template_notify`, which binds a TCP listener and serves every incoming connection through the `handle` async function.

The `handle` function:

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
- Any authentication header (e.g., HTTP Basic Auth, bearer token, shared secret)
- Any HMAC or signature over the template body

`update_block_template` immediately replaces the miner's active work with whatever `BlockTemplate` was received, and dispatches it to all worker threads via `new_work_tx`.

The `listen_block_template_notify` function accepts connections from any source without restriction:

```rust
async fn listen_block_template_notify(&self, addr: SocketAddr) {
    let listener = TcpListener::bind(addr).await.unwrap();
    ...
    conn = listener.accept() => {
        let (stream, _) = match conn { Ok(conn) => conn, ... };
        // no IP check, no auth check
        let conn = server.serve_connection_with_upgrades(stream, handle);
```

The `MinerClientConfig` struct defines `listen: Option<SocketAddr>` as the only configuration for this server — there is no `allowed_ips`, `secret`, or `auth_token` field.

---

### Impact Explanation

An attacker who can reach the miner's notify port (e.g., the miner is bound to `0.0.0.0:8888`, or the attacker has local network access) can:

1. **Inject a stale or low-difficulty fake template**: The miner wastes all hashpower on a block that will never be accepted by the network.
2. **Inject a template with a manipulated cellbase**: The miner mines blocks that pay rewards to the attacker's lock script instead of the operator's.
3. **Continuously inject new fake templates**: Since `update_block_template` replaces the current work on every call, the attacker can keep the miner perpetually chasing invalid work, preventing any valid block submission.

The codebase's own log message acknowledges the severity: *"Otherwise ckb-miner will malfunction and stop submitting valid blocks after a certain period."*

The economic impact is direct: the miner operator loses all mining revenue for the duration of the attack.

---

### Likelihood Explanation

The `listen` option is a documented, supported feature in `ckb-miner.toml`. Operators who configure notify mode (to reduce latency vs. polling) expose this endpoint. If the listen address is `0.0.0.0` or the machine is reachable from the internet or a shared network, any unprivileged peer can exploit this. Even a `127.0.0.1` binding is exploitable by any local process (e.g., a malicious dependency, a co-located service). The attack requires only a single HTTP POST with a valid JSON `BlockTemplate` structure, which is fully documented in the CKB RPC README.

---

### Recommendation

Add authentication to the miner's notify HTTP server. The simplest approach mirrors what the miner already does when connecting *to* the CKB node: HTTP Basic Auth via a shared secret configured in `ckb-miner.toml`. The `handle` function should reject requests that do not present the correct `Authorization` header. Alternatively, restrict accepted connections to a configurable IP allowlist.

---

### Proof of Concept

**Root cause — no auth in `handle`:** [1](#0-0) 

**Listener accepts all connections without source check:** [2](#0-1) 

**`update_block_template` immediately replaces active work:** [3](#0-2) 

**`MinerClientConfig` has no auth field for the notify server:** [4](#0-3) 

**Notify mode is a documented, supported feature:** [5](#0-4) 

**Attack steps:**

1. Operator configures `listen = "0.0.0.0:8888"` in `ckb-miner.toml` and starts `ckb miner`.
2. Attacker sends:
   ```
   POST http://<miner-ip>:8888/ HTTP/1.1
   Content-Type: application/json

   {"work_id":"0x9999","version":"0x0","compact_target":"0x207fffff",
    "current_time":"0x...","number":"0x1","parent_hash":"0x...",
    "cycles_limit":"0xd09dc300","bytes_limit":"0x91c08",
    "uncles_count_limit":"0x2","uncles":[],"transactions":[],
    "proposals":[],"cellbase":{"cycles":null,"data":{...attacker_lock...},"hash":"0x..."},
    "epoch":"0x...","dao":"0x...","extension":null}
   ```
3. `handle` deserializes the payload and calls `client.update_block_template(template)` with no checks.
4. All miner workers immediately switch to mining the attacker-controlled template.
5. Any blocks found are either invalid (wrong parent/DAO) or pay rewards to the attacker's cellbase lock script.

### Citations

**File:** miner/src/client.rs (L234-261)
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

**File:** util/app-config/src/configs/miner.rs (L17-30)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
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

**File:** resource/ckb-miner.toml (L59-60)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"
```
