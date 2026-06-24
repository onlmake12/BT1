Audit Report

## Title
Unauthenticated Miner Notify HTTP Server Allows Reward Redirection — (File: `miner/src/client.rs`)

## Summary
The miner's notify-mode HTTP server (`listen_block_template_notify`) accepts `BlockTemplate` pushes from any caller with no authentication. An attacker who can reach the miner's notify port can POST a crafted template whose cellbase witness contains their own lock script. Because the node's `RewardVerifier` only validates the cellbase **output** lock against the **target block's** previously stored witness — and never validates the current block's witness against the node's own `BlockAssemblerConfig` — the attacker's lock is stored on-chain and used to pay the block reward `PROPOSAL_WINDOW.farthest + 1` blocks later.

## Finding Description

**Step 1 — No authentication in `handle`.**
`miner/src/client.rs` lines 358–369 show the HTTP handler accepts any POST body, deserializes it as a `BlockTemplate`, and immediately calls `update_block_template`. There is no `Authorization` header check, IP allowlist, or any other gate.

**Step 2 — Template replacement is unconditional.**
`update_block_template` (lines 293–312) replaces the current work whenever the incoming `work_id` differs from the current one. An attacker simply uses any `work_id` value different from the live one; the `id == 0` branch also unconditionally accepts updates when the miner has just started.

**Step 3 — `CellbaseVerifier` does not check the lock identity.**
`verification/src/block_verifier.rs` lines 106–124 (`CellbaseVerifier::verify`) only checks that the cellbase witness deserializes as a valid `CellbaseWitness` and that the lock's `hash_type` is in `ENABLED_SCRIPT_HASH_TYPE`. It does **not** compare the lock script against the node's configured `BlockAssemblerConfig`. An attacker-supplied lock with a valid `hash_type` (e.g., `type`) passes this check.

**Step 4 — `RewardVerifier` reads the stored witness of the *target* block, not the current block.**
`util/reward-calculator/src/lib.rs` lines 90–101 (`block_reward_internal`) derive `target_lock` by reading the cellbase witness of the block being finalized (the target block, `PROPOSAL_WINDOW.farthest + 1` blocks in the past). `RewardVerifier` (`contextual_block_verifier.rs` lines 262–271) then enforces that the **current** block's cellbase output lock equals that `target_lock`. It never checks what lock is embedded in the **current** block's own cellbase witness.

**Consequence:** The attacker's lock is stored in the current block's cellbase witness. When that block becomes the target block in the future, `block_reward_internal` reads the attacker's lock as `target_lock` and the reward is paid there.

**Step 5 — Outgoing auth exists; inbound auth does not.**
`parse_authorization` (lines 380–394) supports HTTP Basic Auth for outgoing RPC calls. No equivalent mechanism exists for the inbound notify server, and the `ClientConfig` struct (`util/app-config/src/configs/miner.rs` lines 17–30) provides no field for configuring inbound credentials.

## Impact Explanation
This is a concrete financial attack on CKB miners. Every block mined while the fake template is active pays 100% of the block reward (primary + secondary + tx fees + proposal reward) to the attacker's address. Mining pools and professional operators who run the miner in notify mode with a non-localhost `listen` address are directly exposed. This constitutes damage to the CKB economy (Critical, 15001–25000 points): block rewards are a core economic primitive of the CKB network, and systematic redirection of those rewards undermines miner incentives and network security.

## Likelihood Explanation
Notify mode is opt-in (`listen: Option<SocketAddr>`), but it is the standard configuration for any setup where the CKB node and miner run on separate machines — the common case for mining pools. The `SocketAddr` type accepts `0.0.0.0:PORT`, exposing the port to any reachable host. The exploit requires only a single unauthenticated HTTP POST; no credentials, keys, or privileged access are needed. The attack is repeatable at will and silent (the miner logs no warning about the substituted template).

## Recommendation
Add HTTP Basic Auth to the notify server. The simplest approach reuses the existing `parse_authorization` infrastructure: extract the credential from `config.rpc_url` at startup, store it in `Client`, and in `handle` reject (HTTP 401) any request whose `Authorization` header does not match. Alternatively, add a dedicated `notify_auth` field to `ClientConfig`. The `handle` function must perform this check **before** deserializing or acting on the body.

## Proof of Concept
Precondition: miner configured with `listen = "0.0.0.0:8888"`.

```bash
# Attacker posts a BlockTemplate with their own lock in the cellbase witness.
# The work_id (0x2) just needs to differ from the miner's current work_id.
curl -s -X POST http://<miner-host>:8888/ \
  -H "Content-Type: application/json" \
  -d '{
    "version":"0x0",
    "compact_target":"0x1a08a97e",
    "current_time":"0x...",
    "number":"0x100",
    "epoch":"0x...",
    "parent_hash":"0x...",
    "cycles_limit":"0x...",
    "bytes_limit":"0x...",
    "uncles_count_limit":"0x2",
    "uncles":[], "transactions":[], "proposals":[],
    "cellbase":{
      "cycles":null,
      "data":{
        "cell_deps":[],"header_deps":[],
        "inputs":[{"previous_output":{"index":"0xffffffff","tx_hash":"0x00...00"},"since":"0x100"}],
        "outputs":[{"capacity":"0x...","lock":{"args":"0x<LEGITIMATE_ARGS>","code_hash":"0x...","hash_type":"type"},"type":null}],
        "outputs_data":["0x"],
        "version":"0x0",
        "witnesses":["0x<ATTACKER_CELLBASE_WITNESS_ENCODING_ATTACKER_LOCK>"]
      }
    },
    "dao":"0x...","work_id":"0x2","extension":null
  }'
```

The miner immediately begins mining on this template. The next block it finds and submits will have the attacker's lock in the cellbase witness. `CellbaseVerifier` passes (valid format, valid hash_type). `RewardVerifier` passes (output lock matches the *target* block's witness, which is unrelated). `PROPOSAL_WINDOW.farthest + 1` blocks later, `block_reward_internal` reads the attacker's lock from the stored witness and the full block reward is paid to the attacker.