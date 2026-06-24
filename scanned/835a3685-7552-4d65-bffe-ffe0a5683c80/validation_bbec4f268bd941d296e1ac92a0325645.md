All cited code references check out against the actual repository. Let me confirm each claim:

1. **No height guard**: `execute()` partitions by `is_main_chain()` only — no `header.number() <= last_block.number()` check exists. [1](#0-0) 

2. **MMR sized to `last_block.number()-1`**: `reply_proof` builds the MMR anchored at `last_block.number() - 1`, so any position from a block at a higher height is out of range. [2](#0-1) 

3. **`gen_proof` failure → `InternalError`**: Out-of-range positions cause `gen_proof` to return an error, which maps to `StatusCode::InternalError`. [3](#0-2) 

4. **No ban for `InternalError`**: `should_ban()` only covers codes `400..500`; `InternalError = 500` falls outside and only triggers a warning log. [4](#0-3) 

---

Audit Report

## Title
Missing Ancestor-Height Check in `GetBlocksProofProcess::execute` Allows Unbounded Repeated `InternalError` Without Peer Ban — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
`GetBlocksProofProcess::execute` validates that each hash in `block_hashes` is on the main chain but never verifies that those blocks' heights are ≤ `last_block.number()`. When a higher-height main-chain block hash is supplied, its MMR leaf position exceeds the MMR's range (sized to `last_block.number()-1`), causing `gen_proof` to fail and return `StatusCode::InternalError` (500). Because `should_ban()` only covers codes 400–499, the peer is never banned and can repeat this indefinitely, wasting server I/O and CPU on every request.

## Finding Description
In `execute()`, after confirming `last_hash` is on the main chain, the code partitions `block_hashes` into `found` (on main chain) and `missing` using only `snapshot.is_main_chain()`. For each hash in `found`, it calls `leaf_index_to_pos(header.number())` and appends the result to `positions` — with no check that `header.number() <= last_block.number()`.

`reply_proof` then constructs `snapshot.chain_root_mmr(last_block.number() - 1)`, an MMR covering only leaf indices 0 through `last_block.number()-1`. When `positions` contains a value derived from a block at height > `last_block.number()`, `mmr.gen_proof(items_positions)` returns an error. The function returns `StatusCode::InternalError.with_context(...)`.

Back in `received()`, `status.should_ban()` checks `!(400..500).contains(&code)`. Since `InternalError = 500`, the condition is true and `None` is returned — no ban is issued. The peer receives only a warning log and remains connected, free to repeat the request.

## Impact Explanation
An attacker with a valid P2P connection can sustain a stream of malformed `GetBlocksProof` messages at the maximum rate the network allows. Each message forces the server to perform store lookups for the `last_block` header, fetch headers and uncle data for each "found" block, compute the MMR root (`get_root()` succeeds), and then fail at `gen_proof`. No response is sent to the attacker. Because no ban is ever applied, the attacker can saturate the server's async task queue, degrading or denying proof service to legitimate light clients. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as the light client protocol server is part of the CKB network infrastructure and the attack requires only a P2P connection and two publicly known block hashes.

## Likelihood Explanation
The exploit requires only a valid P2P connection and two main-chain block hashes at different heights — both trivially obtainable from any public node or block explorer. No proof-of-work, key material, or privileged role is needed. The attack is free to repeat without limit because no ban is ever applied.

## Recommendation
In `execute()`, after resolving `last_block`, reject any hash in `block_hashes` whose header number exceeds `last_block.number()`. Such blocks cannot be ancestors of `last_hash` and constitute a malformed request. Return `StatusCode::MalformedProtocolMessage` (400), which falls in the ban range and triggers `nc.ban_peer()`:

```rust
for block_hash in found {
    let header = snapshot
        .get_block_header(&block_hash)
        .expect("header should be in store");
    if header.number() > last_block.number() {
        return StatusCode::MalformedProtocolMessage
            .with_context("block hash is not an ancestor of last_hash");
    }
    positions.push(leaf_index_to_pos(header.number()));
    // ... rest of existing logic
}
```

This converts the server-side `InternalError` (no ban) into a client-side `MalformedProtocolMessage` (ban), closing the free-repeat loop.

## Proof of Concept
1. Mine a chain to height 100.
2. Connect as a peer to the light-client protocol.
3. Send `GetBlocksProof { last_hash = hash of block at height 10, block_hashes = [hash of block at height 50] }`.
4. Observe: server emits a warning log (`InternalError`) and sends no `SendBlocksProof` response.
5. Observe: peer is NOT banned — no disconnect, no `ban_peer` call.
6. Repeat step 3 in a tight loop.
7. Measure: legitimate `GetBlocksProof` requests from a second peer experience increased latency or dropped responses as the server's async task queue is saturated with failing proof-generation attempts.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-85)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
```

**File:** util/light-client-protocol-server/src/lib.rs (L199-215)
```rust
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
```

**File:** util/light-client-protocol-server/src/status.rs (L95-101)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
```
