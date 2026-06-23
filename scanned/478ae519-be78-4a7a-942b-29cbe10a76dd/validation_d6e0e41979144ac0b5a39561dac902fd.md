### Title
Unauthenticated Block Template Injection via Miner Notify-Mode HTTP Server — (`miner/src/client.rs`)

---

### Summary

When the CKB miner is configured in "notify mode," it binds a plain TCP/HTTP listener that accepts `BlockTemplate` pushes from any connecting client with no authentication, IP restriction, or signature check. Any attacker who can reach the listen port can inject a crafted `BlockTemplate` — including one with a modified cellbase transaction — causing the miner to mine and submit blocks that redirect block rewards to the attacker's address.

---

### Finding Description

**Root cause — missing caller/source authorization check in `handle`:**

`listen_block_template_notify` in `miner/src/client.rs` binds a `TcpListener` to the operator-configured `SocketAddr` and spawns a Hyper HTTP server: [1](#0-0) 

Every accepted TCP connection is dispatched to the `handle` free function: [2](#0-1) 

`handle` collects the raw HTTP body, deserializes it as a `BlockTemplate`, and immediately calls `client.update_block_template(template)`. There is **no check** on:
- the remote IP address (no allowlist against the configured CKB node),
- any authentication token or `Authorization` header,
- any HMAC or cryptographic signature over the template body.

`update_block_template` then unconditionally replaces the active work and sends it to all mining workers: [3](#0-2) 

The miner's `Miner::notify_new_work` immediately dispatches the injected work to worker threads, which begin solving PoW on the attacker-supplied template: [4](#0-3) 

Once a nonce is found, `submit_nonce` assembles the block from the injected template's header fields and submits it to the CKB node via `submit_block` RPC: [5](#0-4) 

The `BlockTemplate` type includes a `cellbase` transaction field: [6](#0-5) 

An attacker can craft a template whose `cellbase` output lock script points to the attacker's own address. The node's `submit_block` RPC verifies the header (PoW, parent hash) but does **not** enforce that the cellbase lock script matches the operator's configured `block_assembler`: [7](#0-6) 

The notify mode is activated by setting the `listen` field in `MinerClientConfig`: [8](#0-7) 

The default template comments it out (`# listen = "127.0.0.1:8888"`), but it is a documented, production-intended feature: [9](#0-8) 

---

### Impact Explanation

An attacker who can reach the miner's listen port can:

1. **Redirect mining rewards** — inject a `BlockTemplate` with a cellbase paying to the attacker's lock script. The miner mines and submits the block; the node accepts it (cellbase lock is not constrained by the node); the block reward is paid to the attacker.
2. **Waste hashpower** — inject templates with stale parent hashes or manipulated `compact_target`, causing the miner to mine on dead forks or produce blocks the node will reject, denying the operator their expected revenue.
3. **Censor or inject transactions** — the attacker controls the `transactions` field of the injected template, allowing arbitrary transaction inclusion or exclusion in mined blocks.

The most severe impact is **theft of block rewards** from the miner operator with no on-chain recourse.

---

### Likelihood Explanation

- The feature is opt-in, but it is a documented, production-intended mode explicitly described in the default config and in the startup log message.
- When `listen` is bound to a non-loopback address (e.g., `0.0.0.0:8888`), the attack is reachable from any network peer.
- Even when bound to `127.0.0.1`, any co-located process (shared hosting, container escape, compromised dependency) can exploit it.
- No special privileges are required: a single unauthenticated HTTP POST suffices.
- Mining pools and professional operators are the most likely users of notify mode, making them the highest-value targets.

---

### Recommendation

1. **IP allowlist**: Extract the host from `config.rpc_url` and reject connections whose remote `SocketAddr` does not match.
2. **Shared secret / HMAC**: Require an `Authorization` header (e.g., Bearer token or HMAC-SHA256 over the body) configured alongside `listen`; reject requests that fail verification.
3. **Cellbase lock enforcement**: In `update_block_template`, verify that the received template's cellbase output lock script matches the locally configured `block_assembler` lock script before accepting the work.

---

### Proof of Concept

**Precondition:** Miner is running with `listen = "0.0.0.0:8888"` in `ckb-miner.toml`.

**Steps:**

1. Obtain a valid `BlockTemplate` from the CKB node (e.g., via `get_block_template` RPC) to use as a structural base.
2. Replace the `cellbase.data.outputs[0].lock` field with the attacker's lock script (e.g., secp256k1 lock with attacker's pubkey hash).
3. POST the crafted JSON to the miner's notify endpoint:
   ```
   curl -X POST http://<miner-ip>:8888/ \
     -H 'Content-Type: application/json' \
     -d '<crafted_block_template_json>'
   ```
4. The miner's `handle` function deserializes the body and calls `update_block_template` with no source check.
5. Mining workers begin solving PoW on the injected template.
6. Upon finding a valid nonce, `submit_nonce` assembles the block and calls `submit_block` on the CKB node.
7. The node verifies the header PoW and parent hash, accepts the block, and pays the block reward to the attacker's lock script.

**Expected outcome:** The miner's hashpower is silently redirected to produce blocks whose coinbase reward is paid to the attacker, with no error or warning emitted by the miner process.

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

**File:** tx-pool/src/block_assembler/mod.rs (L748-768)
```rust
#[derive(Clone)]
pub(crate) struct BlockTemplate {
    pub(crate) version: Version,
    pub(crate) compact_target: u32,
    pub(crate) number: BlockNumber,
    pub(crate) epoch: EpochNumberWithFraction,
    pub(crate) parent_hash: Byte32,
    pub(crate) cycles_limit: Cycle,
    pub(crate) bytes_limit: u64,
    pub(crate) uncles_count_limit: u8,

    // option
    pub(crate) uncles: Vec<UncleBlockView>,
    pub(crate) transactions: Vec<TxEntry>,
    pub(crate) proposals: Vec<ProposalShortId>,
    pub(crate) cellbase: TransactionView,
    pub(crate) work_id: u64,
    pub(crate) dao: Byte32,
    pub(crate) current_time: u64,
    pub(crate) extension: Option<Bytes>,
}
```

**File:** rpc/src/module/miner.rs (L260-293)
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
