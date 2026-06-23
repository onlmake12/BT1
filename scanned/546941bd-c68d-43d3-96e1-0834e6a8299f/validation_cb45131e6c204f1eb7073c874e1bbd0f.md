### Title
Unauthenticated Miner Block-Template Injection via Notify HTTP Server - (File: `miner/src/client.rs`)

### Summary
When the CKB miner is configured in "notify mode," it starts a plain HTTP server that accepts `BlockTemplate` POSTs from any source with no authentication whatsoever. An attacker who can reach the miner's listen address can inject a crafted block template, redirecting the miner's hashpower to mine on an attacker-controlled block — including one with a different coinbase lock script (reward address).

### Finding Description

The miner's `spawn_background` method checks whether `config.listen` is set. If so, it spawns `listen_block_template_notify`, which binds a raw TCP listener and serves every incoming HTTP connection through the `handle` function: [1](#0-0) 

The `handle` function that processes every incoming request: [2](#0-1) 

There is no `Authorization` header check, no IP allowlist, no shared secret, and no HMAC verification. Any HTTP POST whose body deserializes as a valid `BlockTemplate` JSON is accepted and immediately forwarded to `update_block_template`: [3](#0-2) 

`update_block_template` atomically replaces `current_work_id` and sends the new `Work` to all mining workers via `new_work_tx`, causing them to immediately begin mining on the injected template.

The `ClientConfig` struct shows `listen` is an optional `SocketAddr` — it can be bound to any interface, including `0.0.0.0`: [4](#0-3) 

The default template even documents this mode as a supported production configuration: [5](#0-4) 

By contrast, the miner's outbound RPC client does implement optional Basic Auth (via `parse_authorization`), but the inbound notify server has no equivalent: [6](#0-5) 

### Impact Explanation

An attacker who can reach the miner's listen port can:

1. **Redirect hashpower**: Inject a `BlockTemplate` with a different `cellbase` transaction whose `lock` script points to the attacker's address. The miner will solve PoW for this template and submit it, sending block rewards to the attacker.
2. **Waste/stall mining**: Inject a template with a stale `parent_hash` or invalid fields. The miner will mine on it and `submit_block` will fail, wasting all hashpower until the next legitimate template arrives.
3. **Persistent hijack**: Because `update_block_template` only updates when `work_id` changes or is zero, an attacker can lock the miner onto a specific work ID, preventing legitimate template updates from taking effect.

### Likelihood Explanation

- The listen address is operator-configured and can be any `SocketAddr`, including `0.0.0.0:8888`.
- No firewall rule is enforced by the code itself.
- The attack requires only network reachability to the miner's listen port — no credentials, no keys, no privileged role.
- The block assembler's `notify` URL list (in `ckb.toml`) is the only intended sender, but the miner server enforces nothing to verify the sender is actually the CKB node. [7](#0-6) 

### Recommendation

- Validate the source IP against a configured allowlist (e.g., only accept from the CKB node's address).
- Add a shared secret / HMAC-SHA256 signature on the `BlockTemplate` payload, verified by the miner before calling `update_block_template`.
- At minimum, bind the notify listener to `127.0.0.1` by default and document that binding to any other interface requires compensating controls.

### Proof of Concept

Assuming miner is configured with `listen = "0.0.0.0:8888"` and the attacker controls a `BlockTemplate` JSON with a modified cellbase lock (attacker's address):

```bash
# Craft a BlockTemplate JSON with attacker's coinbase lock script
# (copy a real template from get_block_template, replace cellbase outputs lock)
curl -X POST http://<miner-ip>:8888/ \
  -H "Content-Type: application/json" \
  -d '{ ...crafted BlockTemplate with attacker coinbase... }'
```

The miner's `handle` function at `miner/src/client.rs:358-369` deserializes the body with no auth check, calls `update_block_template`, and all workers immediately begin mining on the attacker's template. The next solved block is submitted via `submit_block` with the attacker's coinbase, paying block rewards to the attacker's address. [8](#0-7)

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

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

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
