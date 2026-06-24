All code references check out exactly against the actual source. Let me confirm the key findings:

- `listen_block_template_notify` L234–271: confirmed, binds to `SocketAddr`, peer address discarded at L245 (`let (stream, _)`).
- `handle` L358–369: confirmed, zero auth — deserializes any valid `BlockTemplate` JSON and calls `update_block_template` unconditionally.
- `update_block_template` L293–312: confirmed, the only guard is `id != work_id || id == 0`, trivially bypassed since `work_id` is attacker-supplied.
- `parse_authorization` L380–394: confirmed, only wired into outgoing RPC at L77, never into the incoming notify server.
- `submit_nonce` in `miner/src/miner.rs` L140–188: confirmed, assembles and submits the block directly from the injected `Work`.
- SECURITY.md "local network access" exclusion is scoped to the **Websites and Apps** section only — not to blockchain/node components.

---

Audit Report

## Title
Unauthenticated Block Template Injection via Miner Notify HTTP Server — (File: miner/src/client.rs)

## Summary
The miner's notify-mode HTTP server (`listen_block_template_notify`) accepts `BlockTemplate` payloads from any TCP client with no authentication, IP filtering, or source verification. An attacker who can reach the configured listen port can POST a crafted `BlockTemplate` with an attacker-controlled coinbase lock script, causing all mining workers to immediately switch to the injected template. The next valid nonce found submits a block paying the full block reward to the attacker's address.

## Finding Description
`listen_block_template_notify` (`miner/src/client.rs`, L234–271) binds to the configured `SocketAddr` and dispatches every accepted TCP connection to `handle`. The peer address is explicitly discarded at L245 (`let (stream, _) = match conn`), making IP-based filtering impossible at the accept stage.

The `handle` function (L358–369) reads the request body, deserializes it as `BlockTemplate`, and unconditionally calls `client.update_block_template(template)` — with no `Authorization` header check, no shared-secret validation, and no peer-address comparison:

```rust
async fn handle(client: Client, req: Request<hyper::body::Incoming>)
    -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();
    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);  // no auth, no source check
    }
    Ok(Response::new(Empty::new()))
}
```

`update_block_template` (L293–312) contains one guard: it rejects a template whose `work_id` equals the current one and is non-zero. Because `work_id` is a field in the attacker-supplied JSON, the attacker trivially bypasses it by supplying any differing value. On success, `Works::New(work)` is sent to all worker threads via `new_work_tx`, immediately redirecting all hashpower to the injected template.

`parse_authorization` (L380–394) exists in the same file but is wired exclusively to outgoing RPC requests at L77 — it is never consulted for incoming connections on the notify server.

When a worker finds a valid nonce, `submit_nonce` in `miner/src/miner.rs` (L140–188) assembles the block from the injected `Work` and submits it to the CKB node via `submit_block` — with the attacker's coinbase intact and unmodified.

## Impact Explanation
A successfully mined block built from the injected template pays the full coinbase reward to the attacker's lock script. This is direct, concrete theft of block rewards from legitimate miners. This matches **"Vulnerabilities which could easily damage CKB economy" — Critical (15001–25000 points)**. The attack requires no privileges, no keys, and no victim interaction beyond the miner's notify port being reachable.

## Likelihood Explanation
Notify mode is a documented, production-supported feature. Any operator running the CKB node and miner on separate hosts (including cloud deployments) must bind the listen address to a non-loopback interface, making the port network-reachable. The `BlockTemplate` JSON schema is fully public. The exploit is a single unauthenticated HTTP POST — no credentials, no reverse engineering, no race condition. The attacker can re-inject continuously to keep the miner permanently redirected. The SECURITY.md "local network access" exclusion applies only to the Websites and Apps section, not to blockchain node components.

## Recommendation
Inside `handle`, verify the request originates from the configured CKB node address before calling `update_block_template`. The peer address is available from `listener.accept()` at L244–245 and must be threaded through to `handle` rather than discarded. The simplest correct fix is to pass the allowed origin IP (derived from `config.rpc_url`) into `handle` and reject connections whose peer address does not match. Alternatively, require a configurable bearer token in the `Authorization` header, mirroring the `parse_authorization` pattern already used for outgoing RPC calls at L77.

## Proof of Concept
**Preconditions:** Miner running in notify mode with `listen` bound to a network-reachable address (required for any distributed node+miner setup).

```python
import requests, json

attacker_lock = {
    "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
    "hash_type": "type",
    "args": "0x<ATTACKER_PUBKEY_HASH>"
}

# Obtain a real BlockTemplate from get_block_template RPC.
# Replace cellbase output lock with attacker_lock.
# Set work_id to any value != current (attacker controls this field).
fake_template = { ... }  # full BlockTemplate JSON with attacker coinbase

r = requests.post("http://<miner-listen-addr>/", json=fake_template)
assert r.status_code == 200
# All worker threads now receive WorkerMessage::NewWork with injected template.
# Next valid nonce submits a block paying block reward to attacker_lock.
```

**Verification:** After the POST, the miner worker log will show `WorkerMessage::NewWork` carrying the injected template's `pow_hash`. The next `submit_block` RPC call will include a `cellbase` transaction paying the attacker's lock script.