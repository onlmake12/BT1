### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Listener - (`File: miner/src/client.rs`)

### Summary
The CKB miner's "notify mode" HTTP listener accepts block template updates from any TCP client without any authentication or source validation. The `handle` function at `miner/src/client.rs:358-369` directly calls `client.update_block_template(template)` on any well-formed HTTP POST body, regardless of origin. An attacker who can reach the miner's configured listen port can inject a crafted `BlockTemplate` containing an attacker-controlled `cellbase` reward address, causing the miner to mine and submit blocks that pay rewards to the attacker instead of the legitimate operator.

### Finding Description

When the miner is configured in notify mode (`listen = "0.0.0.0:8888"` or any reachable address in `ckb-miner.toml`), `Client::spawn_background` in `miner/src/client.rs` calls `listen_block_template_notify`, which binds a `TcpListener` and registers `handle` as the HTTP service function for every accepted connection:

```rust
// miner/src/client.rs:234-254
async fn listen_block_template_notify(&self, addr: SocketAddr) {
    let listener = TcpListener::bind(addr).await.unwrap();
    ...
    loop {
        let client = self.clone();
        let handle = service_fn(move |req| handle(client.clone(), req));
        tokio::select! {
            conn = listener.accept() => {
                ...
                let conn = server.serve_connection_with_upgrades(stream, handle);
```

The `handle` function contains zero authentication logic:

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

`update_block_template` immediately dispatches the injected work to all mining workers via `new_work_tx`:

```rust
// miner/src/client.rs:293-312
fn update_block_template(&self, block_template: BlockTemplate) {
    let work_id = block_template.work_id.into();
    ...
    if self.current_work_id.fetch_update(...).is_ok() {
        let work: Work = block_template.into();
        if let Err(e) = self.new_work_tx.send(Works::New(work)) { ... }
    }
}
```

The `BlockTemplate` type (`util/jsonrpc-types/src/block_template.rs`) includes a `cellbase` field — the coinbase transaction that encodes the miner reward recipient. An attacker-supplied template with a crafted `cellbase` pointing to the attacker's lock script will cause the miner to solve PoW for a block that pays the block reward to the attacker.

The `listen` field in `ClientConfig` (`util/app-config/src/configs/miner.rs:29`) is a plain `Option<SocketAddr>` with no IP allowlist or credential mechanism. The default template (`resource/ckb-miner.toml:60`) shows it commented out, but when operators enable notify mode (required for low-latency mining), the listener is exposed with no protection.

### Impact Explanation

A successful injection causes the miner to:
1. Abandon the legitimate block template from the CKB node.
2. Mine on the attacker's template, which contains an attacker-controlled `cellbase` reward address.
3. Submit the solved block via `submit_block` RPC to the CKB node — the node performs only consensus/PoW validity checks, not reward-address ownership checks.
4. The block reward (currently ~1917 CKB per block) is permanently paid to the attacker's address.

This is direct, irreversible theft of mining revenue. A sustained attacker who keeps re-injecting templates on every new work cycle can capture 100% of the victim miner's block rewards for as long as the attack persists.

### Likelihood Explanation

- The attack requires only network reachability to the miner's configured `listen` port — no credentials, no keys, no privileged role.
- Operators who enable notify mode for performance reasons (the documented use case) are fully exposed.
- If the listen address is `0.0.0.0` or a public IP (common in pool/datacenter deployments), the attack surface is internet-wide.
- Even a `127.0.0.1` binding is reachable by any co-tenant process, container, or compromised dependency on the same host.
- The attack is silent: the miner logs show normal operation; the only observable anomaly is that mined blocks pay a different address.

### Recommendation

Add source IP allowlisting or a shared-secret token check inside `handle` before calling `update_block_template`. The simplest correct fix mirrors the existing outbound `parse_authorization` pattern: require an `Authorization: Bearer <token>` header on inbound notify requests, where the token is configured in `ClientConfig` alongside `listen`. Reject (return HTTP 401) any request that does not present the correct token.

### Proof of Concept

1. Operator configures `ckb-miner.toml` with `listen = "0.0.0.0:8888"` and starts `ckb miner`.
2. Attacker crafts a valid `BlockTemplate` JSON with `cellbase.data.outputs[0].lock` set to the attacker's lock script.
3. Attacker sends:
   ```
   curl -X POST http://<miner-ip>:8888/ \
     -H "Content-Type: application/json" \
     -d '{"version":"0x0","compact_target":"0x1e083126",...,"cellbase":{"data":{"outputs":[{"capacity":"0x...","lock":{"code_hash":"<attacker_code_hash>","hash_type":"type","args":"<attacker_args>"},"type":null}],...}},...}'
   ```
4. The miner immediately switches to mining the injected template (observable via `current_work_id` change and worker restart).
5. When the miner finds a valid nonce, it calls `submit_block` to the CKB node; the node accepts the block; the block reward is credited to the attacker's address. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** miner/src/client.rs (L234-242)
```rust
    async fn listen_block_template_notify(&self, addr: SocketAddr) {
        let listener = TcpListener::bind(addr).await.unwrap();
        let server = auto::Builder::new(TokioExecutor::new());
        let graceful = GracefulShutdown::new();
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        loop {
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
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

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```

**File:** util/jsonrpc-types/src/block_template.rs (L74-77)
```rust
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
    pub work_id: Uint64,
```
