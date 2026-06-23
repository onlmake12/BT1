### Title
Unauthenticated Miner Notify HTTP Server Allows Coinbase Redirection and Mining Reward Theft — (File: `miner/src/client.rs`)

### Summary

When the CKB miner is configured in notify mode (`listen` option set), it starts an HTTP server via `listen_block_template_notify` that accepts block template push notifications with **zero authentication**. Any attacker who can reach this port can inject a crafted `BlockTemplate` containing an attacker-controlled coinbase lock script. The miner will solve PoW for the injected template and submit it to the node, paying all block rewards to the attacker's address.

### Finding Description

When `config.listen` is set, `spawn_background` starts `listen_block_template_notify`: [1](#0-0) 

`listen_block_template_notify` binds a TCP listener and serves every incoming HTTP connection using the `handle` function with no authentication layer: [2](#0-1) 

The `handle` function accepts any HTTP POST body, deserializes it as `BlockTemplate`, and immediately calls `update_block_template` — no token, no IP check, no signature: [3](#0-2) 

`update_block_template` dispatches the attacker-controlled template directly to mining workers via `new_work_tx`: [4](#0-3) 

The miner then solves PoW for the injected template and submits it via `submit_block`. The node's `submit_block` handler validates consensus rules (header, PoW, parent) but does **not** constrain the coinbase lock script — miners are free to set any lock script for their coinbase output: [5](#0-4) 

The `ClientConfig` struct shows `listen` is a supported, documented field: [6](#0-5) 

The default config shows it commented out but with a concrete example address, confirming it is a supported production configuration: [7](#0-6) 

### Impact Explanation

An attacker who can reach the miner's notify listen port can inject a `BlockTemplate` whose `cellbase` output uses the attacker's lock script. The miner will solve PoW for this template and submit it. The CKB node accepts the block (all consensus rules pass), and the block reward is paid to the attacker. This is **direct, permanent theft of mining rewards** — every block mined while the attacker's template is active pays the coinbase to the attacker. Additionally, the attacker can inject stale or low-difficulty templates to waste hashpower or cause the miner to submit invalid blocks, degrading mining operations.

### Likelihood Explanation

The notify mode is a supported, documented production feature. If the operator binds `listen` to a non-localhost address (e.g., to allow the CKB node and miner to run on separate machines — a common deployment), any network-reachable attacker can exploit this with a single HTTP POST. Even with a localhost binding, any process on the same machine (e.g., a compromised co-located service) can exploit it. No credentials, keys, or privileged access are required.

### Recommendation

Add a shared-secret authentication mechanism to the notify HTTP server. The CKB node's block assembler `notify` POST (in `tx-pool/src/block_assembler/mod.rs`) and the miner's listener should share a configurable token sent as an HTTP header (e.g., `Authorization: Bearer <token>`). The `handle` function must reject requests missing or presenting an incorrect token before deserializing the body. Alternatively, enforce that `listen` only accepts connections from the configured CKB node's IP address.

### Proof of Concept

With the miner configured as `listen = "0.0.0.0:8888"`, an attacker sends:

```
POST http://<miner-host>:8888/ HTTP/1.1
Content-Type: application/json

{
  "work_id": "0x1",
  "current_time": "0x174a3b2c000",
  "compact_target": "0x1e083126",
  "dao": "0xb5a3e047474401001bc476b9ee573000c0c387962a38000000febffacf030000",
  "epoch": "0x7080018000001",
  "parent_hash": "<current tip hash>",
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
        "since": "0x0"}],
      "outputs": [{"capacity": "0x18e64b61cf",
        "lock": {"code_hash": "<secp256k1 code hash>",
                 "hash_type": "type",
                 "args": "<ATTACKER_LOCK_ARGS>"},
        "type": null}],
      "outputs_data": ["0x"], "version": "0x0",
      "witnesses": ["0x5500000010000000550000005500000041000000..."]
    }
  },
  "version": "0x0"
}
```

The miner accepts this template, solves PoW, and submits a valid block to the CKB node. The block reward is paid to the attacker's address. The `handle` function performs no authentication check before calling `update_block_template`. [3](#0-2)

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

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```
