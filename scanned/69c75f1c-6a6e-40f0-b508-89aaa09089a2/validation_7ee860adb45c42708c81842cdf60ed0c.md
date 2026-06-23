### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Listener - (File: `miner/src/client.rs`)

### Summary
The CKB miner's HTTP notify listener (`handle` function in `miner/src/client.rs`) accepts block template push notifications from any anonymous caller without any authentication or source validation. Any attacker who can reach the miner's configured listen address can inject a crafted `BlockTemplate`, causing the miner to mine on an attacker-controlled template — including one with a substituted coinbase lock script that redirects block rewards to the attacker.

### Finding Description
When the CKB miner is configured in "notify mode" (i.e., `config.listen` is set), `spawn_background` starts an HTTP server via `listen_block_template_notify` that binds to the configured address and routes all incoming requests through the `handle` function:

```rust
// miner/src/client.rs, lines 358–369
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

This function:
1. Accepts any HTTP request from any source.
2. Attempts to deserialize the body as a `BlockTemplate`.
3. On success, calls `client.update_block_template(template)`, replacing the miner's active work.
4. Returns an empty `200 OK` response unconditionally.

There is no IP allowlist check, no bearer token, no HMAC signature, and no shared secret. The `parse_authorization` helper that exists in the same file (lines 380–394) is used exclusively when the miner **sends** outbound requests to the CKB node RPC — it is never consulted in the inbound `handle` path. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
An attacker who can reach the miner's HTTP listen address can:

1. **Redirect block rewards**: Inject a `BlockTemplate` with a different `cellbase` output lock script (i.e., a different `args` field pointing to the attacker's public key hash). The miner will assemble and solve PoW on this template. If the block is accepted by the network, the coinbase reward is paid to the attacker's address, not the legitimate miner's.
2. **Waste hashpower / deny mining**: Continuously push invalid or empty templates, causing the miner to work on blocks that will be rejected by the network, effectively halting the miner's revenue.
3. **Stale-work griefing**: Push a template with a stale `parent_hash`, causing the miner to produce orphan blocks.

The impact is direct theft of mining rewards and/or denial of mining service — both are concrete, measurable financial harms. [4](#0-3) 

### Likelihood Explanation
- The miner's notify listen address is explicitly logged at startup: `ckb_logger::info!("listen notify mode : {}", addr)`, making it discoverable from node logs or by port scanning.
- The official documentation instructs operators to configure `notify = ["http://<addr>"]` in `[block_assembler]`, meaning the feature is intended for production use.
- No special privileges are required — a standard HTTP POST with a JSON body is sufficient.
- The attacker only needs network reachability to the miner's listen port, which is realistic in shared hosting, cloud environments, or misconfigured firewalls. [5](#0-4) 

### Recommendation
Add source authentication to the `handle` function before processing any incoming block template. Options include:

- **Shared secret / bearer token**: Require a configurable `Authorization` header on inbound notify requests and reject requests that do not match.
- **IP allowlist**: Bind the notify listener to `127.0.0.1` by default and/or validate that the request originates from the configured CKB node's IP.
- **Mutual TLS**: Require a client certificate from the CKB node when using the notify channel.

At minimum, the default bind address for the notify listener should be `127.0.0.1` (loopback only), not `0.0.0.0`, and the documentation should warn operators about the risk of exposing this port.

### Proof of Concept
Attacker steps (no privileges required):

1. Discover the miner's notify listen address (e.g., from logs or port scan).
2. Craft a `BlockTemplate` JSON with a modified `cellbase.data.outputs[0].lock.args` pointing to the attacker's blake160 pubkey hash.
3. Send:
```
POST http://<miner-listen-addr>/ HTTP/1.1
Content-Type: application/json

{ "version": "0x0", "compact_target": "0x1e083126", ..., "cellbase": { "data": { "outputs": [{ "capacity": "0x18e64efc04", "lock": { "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8", "hash_type": "type", "args": "<ATTACKER_PUBKEY_HASH>" }, "type": null }], ... } }, ... }
```
4. The miner's `handle` function deserializes the template and calls `update_block_template`, replacing the legitimate miner's active work.
5. The miner solves PoW on the injected template and submits it via `submit_block`.
6. If accepted, the block reward is paid to the attacker's address. [1](#0-0) [6](#0-5)

### Citations

**File:** miner/src/client.rs (L183-201)
```rust
    pub(crate) fn submit_block(&self, work_id: &str, block: Block) -> Result<(), RpcError> {
        let parent = block.header().raw().parent_hash();
        let future = self
            .send_submit_block_request(work_id, block)
            .and_then(parse_response::<H256>);

        if self.config.block_on_submit {
            self.handle.block_on(future).map(|_| ())
        } else {
            let sender = self.new_work_tx.clone();
            self.handle.spawn(async move {
                if let Err(e) = future.await {
                    error!("rpc call submit_block error: {:?}", e);
                    sender.send(Works::FailSubmit(parent)).unwrap()
                }
            });
            Ok(())
        }
    }
```

**File:** miner/src/client.rs (L204-231)
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
