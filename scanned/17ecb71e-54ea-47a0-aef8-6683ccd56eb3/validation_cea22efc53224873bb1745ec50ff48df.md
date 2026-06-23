### Title
Unauthenticated HTTP Endpoint Allows Any Peer to Inject Fake Block Templates into the CKB Miner — (`miner/src/client.rs`)

---

### Summary

When the CKB miner is configured in "notify mode" (`[miner.client] listen = "..."`), it opens an HTTP server that is designed to receive block templates exclusively from the trusted CKB node. However, the `handle` function that processes incoming HTTP requests performs **no authentication or source verification whatsoever**. Any HTTP client that can reach the miner's listen address can POST a crafted `BlockTemplate` JSON payload and immediately redirect the miner's PoW computation to an attacker-controlled block — including one with a different coinbase reward recipient.

---

### Finding Description

**Root cause — `miner/src/client.rs`, lines 358–369:**

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

The handler accepts any TCP connection, reads the body, deserializes it as a `BlockTemplate`, and immediately calls `update_block_template` — with no IP allowlist check, no shared secret, no HMAC, and no signature verification. The remote peer's identity is never inspected.

**Listener setup — `miner/src/client.rs`, lines 234–271 (`listen_block_template_notify`):**

```rust
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
                let conn = server.serve_connection_with_upgrades(stream, handle);
                tokio::spawn(async move { ... conn.await ... });
            },
```

Every accepted TCP connection is handed directly to the unauthenticated `handle` function. The `(stream, _)` destructuring discards the remote `SocketAddr` entirely — it is never used for any filtering.

**Template injection path — `miner/src/client.rs`, lines 293–312 (`update_block_template`):**

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
    if self.current_work_id.fetch_update(..., updated).is_ok() {
        let work: Work = block_template.into();
        if let Err(e) = self.new_work_tx.send(Works::New(work)) { ... }
    }
}
```

The template is accepted whenever its `work_id` differs from the current one, or the current `work_id` is `0`. An attacker trivially satisfies this by supplying any `work_id` value not currently in use. Once accepted, the `Work` derived from the attacker's template is sent to all mining worker threads, which immediately begin hashing it.

**Submission path:** When a worker finds a valid nonce, `submit_block` is called, which POSTs the solved block (derived from the attacker's template) to the CKB node's `submit_block` RPC. If the attacker's template was structurally valid (correct parent hash, valid transactions, attacker-chosen coinbase), the CKB node accepts it as a legitimate block.

---

### Impact Explanation

1. **Mining reward theft**: An attacker who crafts a valid `BlockTemplate` (copying the legitimate parent hash and transactions but substituting their own lock script as the coinbase recipient) and injects it before the miner finds a nonce causes the miner to submit a block that pays the block reward to the attacker's address. The miner operator loses the entire block reward for that block.

2. **Sustained mining disruption / DoS**: By continuously POSTing templates with incrementing `work_id` values, an attacker resets the miner's work on every injection, preventing it from ever completing a valid PoW solution. This is a targeted denial-of-service against the miner's economic output.

3. **Transaction censorship**: The attacker can inject templates that omit specific transactions, effectively censoring them from blocks produced by the targeted miner.

---

### Likelihood Explanation

- **Notify mode is a documented, supported feature** (`resource/ckb-miner.toml` shows `# listen = "127.0.0.1:8888"`). Operators who enable it for performance reasons are immediately exposed.
- **No privilege required**: The attacker only needs TCP connectivity to the miner's listen address. If the operator binds to `0.0.0.0` or any non-loopback address (common in pool/datacenter setups), the endpoint is reachable from the network.
- **Even on localhost**, any co-resident process (malicious software, compromised dependency, container escape) can exploit this.
- **Exploitation is trivial**: a single `curl -X POST -d '<crafted_template_json>' http://<miner_listen>/` is sufficient.

---

### Recommendation

Add source authentication to the notify HTTP endpoint. The simplest correct fix is a shared-secret token checked on every incoming request:

1. Add a `notify_token: Option<String>` field to `ClientConfig` (`util/app-config/src/configs/miner.rs`).
2. In `handle`, extract the `Authorization` header (or a custom `X-Notify-Token` header) and compare it to the configured token using a constant-time comparison. Reject requests that do not match.
3. Alternatively, bind the listener exclusively to `127.0.0.1` and enforce this in configuration validation, combined with an IP allowlist check against the CKB node's address extracted from `rpc_url`.

The `parse_authorization` function already exists in `miner/src/client.rs` (lines 380–394) for the outbound RPC client — the same pattern should be applied inbound.

---

### Proof of Concept

**Preconditions**: Miner is running with `listen = "127.0.0.1:8888"` (or any reachable address).

**Steps**:

1. Obtain a valid recent block template from the CKB node (or construct one with the correct `parent_hash` and `epoch`).
2. Replace the `cellbase` transaction's output lock script `args` with the attacker's lock script args (the attacker's address).
3. POST the crafted template to the miner's listen address:
   ```
   curl -X POST http://127.0.0.1:8888/ \
     -H "Content-Type: application/json" \
     -d '<crafted_block_template_json>'
   ```
4. The `handle` function deserializes the payload and calls `update_block_template` with no authentication check.
5. The miner's workers immediately begin hashing the attacker's template.
6. When a valid nonce is found, `submit_block` sends the block to the CKB node, which accepts it and pays the block reward to the attacker's address.

**Expected outcome**: The miner operator's PoW computation is redirected to produce a block that pays the block reward to the attacker. The miner operator receives nothing for that block. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
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
