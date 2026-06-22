### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Server — (`miner/src/client.rs`)

### Summary

The CKB miner's "notify mode" HTTP server accepts block template updates from any HTTP client without any authentication or authorization check. When a miner operator configures `listen` in `MinerClientConfig`, the miner binds an HTTP server and calls `update_block_template()` on every incoming POST body that deserializes as a `BlockTemplate`. An attacker who can reach that address can silently replace the miner's active work with a crafted template, redirecting mining rewards to an attacker-controlled address or causing a permanent mining denial-of-service.

### Finding Description

When `config.listen` is set, `Client::spawn_background` starts `listen_block_template_notify`, which binds a `TcpListener` and dispatches every accepted connection to the `handle` function: [1](#0-0) 

The `handle` function is: [2](#0-1) 

There is no IP allowlist check, no shared secret, no HMAC, and no token validation of any kind. Any HTTP client that can reach the bound address and POST a valid JSON `BlockTemplate` body will cause `update_block_template` to be called: [3](#0-2) 

`update_block_template` sends the injected `Work` directly to the worker threads via `new_work_tx`, replacing the current mining target. The update condition is:

```rust
if id != work_id || id == 0 { Some(work_id) } else { None }
```

An attacker can always trigger an update by sending a template with `work_id = 0` (the `id == 0` branch is always true at startup) or by incrementing `work_id` on each injection to stay ahead of the legitimate node.

The `BlockTemplate` type accepted by the handler is the full JSON-serializable struct including `cellbase` (the coinbase transaction): [4](#0-3) 

The attacker can obtain a legitimate template from the public `get_block_template` RPC, replace the `cellbase` lock script with their own address, and POST it to the miner. The miner will mine on the attacker's template and submit the solved block to the node. Because CKB consensus does not restrict who the coinbase lock script belongs to, the node will accept the block and the mining reward will be paid to the attacker.

### Impact Explanation

**Severity: HIGH**

Two concrete outcomes:

1. **Mining reward theft**: Attacker replaces the cellbase lock script with their own. The miner unknowingly mines and submits a valid block paying the reward to the attacker. The node accepts it because the block is otherwise consensus-valid. The legitimate operator loses the entire block reward.

2. **Permanent mining DoS**: Attacker continuously injects templates with an invalid `parent_hash` or an impossibly high `compact_target`. The miner wastes all hash power on unsolvable or unsubmittable work. Because the attacker can keep incrementing `work_id`, they can outpace any legitimate template update indefinitely.

Both outcomes are permanent and unrecoverable without operator intervention (restarting the miner or disabling notify mode).

### Likelihood Explanation

**Likelihood: MEDIUM**

- Notify mode is opt-in (`config.listen` must be set), but it is the documented production mode for operators running the miner on a separate machine from the CKB node.
- The `listen` field is a `SocketAddr`, so operators commonly bind it to a non-loopback address (e.g., `0.0.0.0:PORT`) to receive pushes from a remote CKB node.
- No special privileges are required. Any network-reachable attacker (same LAN, cloud VPC peer, or internet if the port is exposed) can exploit this with a single HTTP POST.
- The CKB node's block assembler `notify` config documents the URL format, making the expected port and path publicly known. [5](#0-4) 

### Recommendation

Add an authentication mechanism to the notify HTTP handler. The simplest approach is a shared secret token configured alongside `listen`:

1. Add a `notify_token: Option<String>` field to `MinerClientConfig`.
2. In `handle`, extract the `Authorization` header (e.g., `Bearer <token>`) and compare it in constant time against the configured token. Reject with `401 Unauthorized` if absent or mismatched.
3. Document that operators **must** set `notify_token` whenever `listen` is bound to a non-loopback address.

Alternatively, restrict `listen` to loopback-only addresses and reject bind attempts to non-loopback addresses with a hard error, forcing the CKB node and miner to run on the same host.

### Proof of Concept

**Preconditions**: Miner is running in notify mode with `listen = "0.0.0.0:18114"` (or any reachable address). Attacker has network access to that port.

**Steps**:

1. Fetch a legitimate block template from the CKB node's public RPC:
   ```bash
   curl -X POST http://<ckb-node>:8114 \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","method":"get_block_template","params":[],"id":1}'
   ```

2. Take the `result` JSON object, replace `cellbase.data.outputs[0].lock` with the attacker's lock script, and set `work_id` to `"0x0"`.

3. POST the modified template to the miner's notify server:
   ```bash
   curl -X POST http://<miner-host>:18114 \
     -H 'Content-Type: application/json' \
     -d '<modified_template_json>'
   ```

4. The miner's `handle` function deserializes the body and calls `update_block_template` with no authentication check. [2](#0-1) 

5. `update_block_template` sends `Works::New(work)` to all worker threads. Workers begin mining on the attacker's cellbase. [3](#0-2) 

6. When a worker finds a valid nonce, `submit_nonce` calls `client.submit_block`, which submits the block to the CKB node. The node verifies consensus rules (PoW, header, transactions) — all of which pass — and accepts the block, paying the reward to the attacker's address. [6](#0-5) 

**For continuous DoS**: Repeat step 3 in a loop, incrementing `work_id` by 1 each time. The miner will never complete a valid block for the legitimate operator.

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

**File:** util/jsonrpc-types/src/block_template.rs (L13-98)
```rust
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct BlockTemplate {
    /// Block version.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub version: Version,
    /// The compacted difficulty target for the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub compact_target: Uint32,
    /// The timestamp for the new block.
    ///
    /// CKB node guarantees that this timestamp is larger than the median of the previous 37 blocks.
    ///
    /// Miners can increase it to the current time. It is not recommended to decrease it, since it may violate the median block timestamp consensus rule.
    pub current_time: Timestamp,
    /// The block number for the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub number: BlockNumber,
    /// The epoch progress information for the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub epoch: EpochNumberWithFraction,
    /// The parent block hash of the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub parent_hash: H256,
    /// The cycles limit.
    ///
    /// Miners must keep the total cycles below this limit, otherwise, the CKB node will reject the block
    /// submission.
    ///
    /// It is guaranteed that the block does not exceed the limit if miners do not add new
    /// transactions to the block.
    pub cycles_limit: Cycle,
    /// The block serialized size limit.
    ///
    /// Miners must keep the block size below this limit, otherwise, the CKB node will reject the block
    /// submission.
    ///
    /// It is guaranteed that the block does not exceed the limit if miners do not add new
    /// transaction commitments.
    pub bytes_limit: Uint64,
    /// The uncle count limit.
    ///
    /// Miners must keep the uncles count below this limit, otherwise, the CKB node will reject the
    /// block submission.
    pub uncles_count_limit: Uint64,
    /// Provided valid uncle blocks candidates for the new block.
    ///
    /// Miners must include the uncles marked as `required` in the assembled new block.
    pub uncles: Vec<UncleTemplate>,
    /// Provided valid transactions which can be committed in the new block.
    ///
    /// Miners must include the transactions marked as `required` in the assembled new block.
    pub transactions: Vec<TransactionTemplate>,
    /// Provided proposal ids list of transactions for the new block.
    pub proposals: Vec<ProposalShortId>,
    /// Provided cellbase transaction template.
    ///
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
    pub work_id: Uint64,
    /// Reference DAO field.
    ///
    /// This field is only valid when miners use all and only use the provided transactions in the
    /// template. Two fields must be updated when miners want to select transactions:
    ///
    /// * `S_i`, bytes 16 to 23
    /// * `U_i`, bytes 24 to 31
    ///
    /// See RFC [Deposit and Withdraw in Nervos DAO](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0023-dao-deposit-withdraw/0023-dao-deposit-withdraw.md#calculation).
    pub dao: Byte32,
    /// The extension for the new block.
    ///
    /// This is a field introduced in [CKB RFC 0031]. Since the activation of [CKB RFC 0044], this
    /// field is at least 32 bytes, and at most 96 bytes. The consensus rule of first 32 bytes is
    /// defined in the RFC 0044.
    ///
    /// [CKB RFC 0031]: https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0031-variable-length-header-field/0031-variable-length-header-field.md
    /// [CKB RFC 0044]: https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0044-ckb-light-client/0044-ckb-light-client.md
    #[serde(default)]
    pub extension: Option<JsonBytes>,
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
