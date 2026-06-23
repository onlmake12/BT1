### Title
Miner Notify-Mode HTTP Listener Accepts Block Templates from Any Originator Without Source Verification — (`miner/src/client.rs`)

### Summary

The CKB miner's notify-mode HTTP listener (`handle` function in `miner/src/client.rs`) accepts `BlockTemplate` payloads from any HTTP client without verifying the originator's identity. An unprivileged network-reachable attacker can POST a crafted `BlockTemplate` with an attacker-controlled cellbase lock script, causing the miner to expend its full hashpower on a block that pays the mining reward to the attacker's address instead of the legitimate miner's address.

### Finding Description

When the miner is configured in notify mode (`config.listen` is set), `Client::spawn_background` starts an HTTP server that calls `handle` for every incoming request: [1](#0-0) 

The `handle` function is the receiving endpoint: [2](#0-1) 

It deserializes the body as a `BlockTemplate` and immediately calls `client.update_block_template(template)` with **zero verification of the HTTP request's source address, authentication header, or any other originator identity check**. There is no IP allowlist, no shared secret, no HMAC, and no TLS client certificate.

`update_block_template` then unconditionally dispatches the attacker-supplied work to all mining workers if the `work_id` differs from the current one (or equals `0`, which the attacker controls): [3](#0-2) 

The `BlockTemplate` contains the full cellbase transaction, including the lock script that determines who receives the block reward. The `BlockAssemblerConfig` sets this lock script on the node side: [4](#0-3) 

The `submit_block` RPC handler on the node verifies the header (PoW, timestamp, parent) and processes the block through the chain, but it does **not** verify that the cellbase lock script in the submitted block matches the configured `block_assembler`: [5](#0-4) 

The `work_id` string passed to `submit_block` is used only for logging and is never validated against any stored state: [6](#0-5) 

**End-to-end exploit path:**

1. Miner is started with `listen = "0.0.0.0:PORT"` in `ckb-miner.toml`.
2. Attacker crafts a `BlockTemplate` JSON payload identical to a legitimate template except the cellbase lock script is replaced with the attacker's own lock script (attacker's address).
3. Attacker sends `HTTP POST` to `http://<miner-ip>:PORT/` with the crafted payload.
4. `handle()` deserializes it and calls `update_block_template()` — no source check occurs.
5. `update_block_template()` sends `Works::New(work)` to all workers; workers begin mining on the attacker's template.
6. When a valid nonce is found, `submit_nonce` calls `client.submit_block(&work.work_id.to_string(), block.data())`.
7. The node's `submit_block` RPC accepts the block (valid PoW, valid parent), and the block reward is paid to the attacker's address. [7](#0-6) 

### Impact Explanation

**Theft of mining rewards.** The attacker redirects 100% of the miner's hashpower to produce blocks that pay the coinbase reward to the attacker's lock script. The legitimate miner performs all the computational work and bears all the electricity cost, while the attacker receives the block reward. This is a direct, concrete financial loss proportional to the miner's hashrate and the duration of the attack. No funds already on-chain are moved, but all future block rewards produced during the attack period are stolen.

### Likelihood Explanation

- Notify mode is an explicitly documented and supported operational mode, advertised in the miner startup log and in `ckb.toml` comments. [8](#0-7) 
- If the miner binds to `0.0.0.0` (common in mining farm deployments where the node and miner run on separate machines), the listener is reachable by any host on the same network segment or, if firewalled incorrectly, from the internet.
- The attack requires only a single unauthenticated HTTP POST — no cryptographic material, no privileged access, no prior interaction with the victim.
- The attacker can sustain the attack indefinitely by re-sending the crafted template whenever the miner fetches a fresh one from the node (the miner does an initial `blocking_fetch_block_template()` on startup, but subsequent updates in notify mode come only via the HTTP push channel). [9](#0-8) 

### Recommendation

The `handle` function must verify that the incoming HTTP request originates from the configured CKB node. Concrete options:

1. **IP allowlist**: Compare the request's remote IP against the configured node RPC URL's host. Reject requests from any other source.
2. **Shared secret / Bearer token**: Require a configurable `Authorization` header on the notify endpoint; the node's block assembler includes the same token when POSTing to `notify` URLs.
3. **Bind to loopback only by default**: Change the default listen address to `127.0.0.1` and document that binding to `0.0.0.0` requires additional network-level access controls.

The node side (`BlockAssembler::notify`) should also be hardened to include an HMAC or token in its outbound notify requests so the miner can authenticate them: [10](#0-9) 

### Proof of Concept

**Preconditions:**
- CKB miner is running in notify mode: `listen = "0.0.0.0:8888"` in `ckb-miner.toml`.
- The node's `block_assembler` is configured with the legitimate miner's lock script.

**Steps:**

1. Obtain a valid `BlockTemplate` from the node (public RPC, no auth required by default):
   ```
   curl -X POST http://<node-ip>:8114/ -H 'Content-Type: application/json' \
     -d '{"id":1,"jsonrpc":"2.0","method":"get_block_template","params":[null,null,null]}'
   ```

2. Replace the `cellbase.data.outputs[0].lock` field with the attacker's lock script (attacker's `code_hash`, `hash_type`, `args`).

3. Set `work_id` to `"0x0"` (or any value different from the miner's current `current_work_id`) to guarantee `update_block_template` dispatches the work.

4. POST the crafted template to the miner's notify listener:
   ```
   curl -X POST http://<miner-ip>:8888/ -H 'Content-Type: application/json' \
     -d '<crafted_block_template_json>'
   ```

5. The miner's workers immediately switch to mining on the attacker's template. When a valid nonce is found, the miner submits the block via `submit_block` RPC. The block is accepted by the node, and the block reward is credited to the attacker's address.

### Citations

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

**File:** util/app-config/src/configs/tx_pool.rs (L53-82)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct BlockAssemblerConfig {
    /// The miner lock script code hash.
    pub code_hash: H256,
    /// The miner lock script args.
    pub args: JsonBytes,
    /// An arbitrary message to be added into the cellbase transaction.
    pub message: JsonBytes,
    /// The miner lock script hash type.
    pub hash_type: ScriptHashType,
    /// Use ckb binary version as message prefix to identify the block miner client (default true, false to disable it).
    #[serde(default = "default_use_binary_version_as_message_prefix")]
    pub use_binary_version_as_message_prefix: bool,
    /// A field to store the block miner client version, non-configurable options.
    #[serde(skip)]
    pub binary_version: String,
    /// A field to control update interval millis
    #[serde(default = "default_update_interval_millis")]
    pub update_interval_millis: u64,
    /// Notify url
    #[serde(default)]
    pub notify: Vec<Url>,
    /// Notify scripts
    #[serde(default)]
    pub notify_scripts: Vec<String>,
    /// Notify timeout
    #[serde(default = "default_notify_timeout_millis")]
    pub notify_timeout_millis: u64,
}
```

**File:** rpc/src/module/miner.rs (L260-298)
```rust
    fn submit_block(&self, work_id: String, block: Block) -> Result<H256> {
        let block: packed::Block = block.into();
        let block: Arc<core::BlockView> = Arc::new(block.into_view());
        let header = block.header();
        debug!(
            "start to submit block, work_id = {}, block = #{}({})",
            work_id,
            block.number(),
            block.hash()
        );

        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();

        // Verify header
        HeaderVerifier::new(snapshot, consensus)
            .verify(&header)
            .map_err(|err| handle_submit_error(&work_id, &err))?;
        if self
            .shared
            .snapshot()
            .get_block_header(&block.parent_hash())
            .is_none()
        {
            let err = format!(
                "Block parent {} of {}-{} not found",
                block.parent_hash(),
                block.number(),
                block.hash()
            );

            return Err(handle_submit_error(&work_id, &err));
        }

        // Verify and insert block
        let is_new = self
            .chain
            .blocking_process_block(Arc::clone(&block))
            .map_err(|err| handle_submit_error(&work_id, &err))?;
```

**File:** miner/src/miner.rs (L140-188)
```rust
    fn submit_nonce(&mut self, pow_hash: Byte32, work: Work, nonce: u128) {
        self.notify_workers(WorkerMessage::Stop);
        let raw_header = work.block.header().raw();
        let header = Header::new_builder().raw(raw_header).nonce(nonce).build();
        let block = work
            .block
            .as_advanced_builder()
            .header(header.into_view())
            .build();
        let block_hash = block.hash();
        let parent_hash = block.parent_hash();

        if self.legacy_work.contains(&parent_hash) {
            debug!(
                "uncle {} pow_hash: {:#x}, header: {}",
                block.number(),
                pow_hash,
                block.header()
            );
            self.notify_workers(WorkerMessage::Start);
            return;
        } else {
            debug!(
                "block {} pow_hash: {:#x}, header: {}",
                block.number(),
                pow_hash,
                block.header()
            );
        }

        self.legacy_work.put(parent_hash, ());
        if self.stderr_is_tty {
            debug!("Found! #{} {:#x}", block.number(), block_hash);
        } else {
            info!("Found! #{} {:#x}", block.number(), block_hash);
        }

        // submit block and poll new work
        {
            if let Err(e) = self
                .client
                .submit_block(&work.work_id.to_string(), block.data())
            {
                self.legacy_work.pop(&block.parent_hash());
                error!("rpc call submit_block error: {:?}", e);
            }
            self.client.blocking_fetch_block_template();
            self.notify_workers(WorkerMessage::Start);
        }
```

**File:** resource/ckb.toml (L270-273)
```text
# # Block assembler will notify new block template through http post to specified endpoints when update
# notify = ["http://127.0.0.1:8888"]
# # Execute command when the block template changes, first arg is block template.
# notify_scripts = ["your_notify_scripts.sh"]
```

**File:** tx-pool/src/block_assembler/mod.rs (L683-712)
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
