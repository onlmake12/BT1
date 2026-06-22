The code is concrete and traceable. Here is the analysis:

**Tracing the path:**

In `execute()`, the only height-related check is whether `last_hash` is on the main chain. There is no check that blocks in `block_hashes` have a height ≤ `last_block.number()`. [1](#0-0) 

Any block hash that passes `is_main_chain()` is placed in `found` and its `leaf_index_to_pos(header.number())` is unconditionally appended to `positions`. [2](#0-1) 

`reply_proof` is then called with `last_block` (height N) and `positions` (which may contain `pos(M)` where M > N). [3](#0-2) 

Inside `reply_proof`, the MMR is constructed for `last_block.number() - 1` (i.e., sized for N−1), then `gen_proof(items_positions)` is called with `pos(M)` where M > N−1, which fails and returns `StatusCode::InternalError`. [4](#0-3) 

**The ban check:**

`should_ban()` only bans status codes in the range `400..500`. `InternalError = 500` falls outside that range, so no ban is issued. [5](#0-4) 

**Conclusion:** The path is concrete and locally testable. The missing ancestor-height validation is a real gap, and the `InternalError` path provably does not ban the peer. However, the actual resource consumed per request is low — the server fetches a few headers from the store and then fails fast on the MMR bounds check. There is no expensive computation (no proof is generated, no MMR traversal completes). The peer is not banned, but the server only logs a warning (`should_warn()` returns true for 5xx) and moves on. The impact is repeated unnecessary work at very low cost per request, with no amplification factor.

This matches the stated scope ("suboptimal state storage mechanism") but the impact is bounded: it is a missing validation + missing ban, not a resource-exhaustion vulnerability with meaningful amplification. The finding is real but its severity is low within the medium scope.

---

### Title
Missing ancestor-height validation in `GetBlocksProofProcess::execute` allows unbanned peers to trigger repeated `InternalError` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary
A remote peer can send a `GetBlocksProof` message where `last_hash` points to block at height N and `block_hashes` contains a hash of a block at height M > N (both on the main chain). The server accepts both as valid, computes `leaf_index_to_pos(M)`, then calls `reply_proof` with an MMR sized for N−1. `gen_proof(pos(M))` fails, returning `StatusCode::InternalError` (500). Because `should_ban()` only acts on 4xx codes, the peer is never banned and can repeat this indefinitely.

### Finding Description
`execute()` partitions `block_hashes` by `is_main_chain()` but never checks that each found block's height is ≤ `last_block.number()`. [6](#0-5) 

`reply_proof` builds the MMR as `chain_root_mmr(last_block.number() - 1)`, so any position derived from a block taller than `last_block` is out of bounds. [4](#0-3) 

`should_ban()` only covers the 400–499 range; `InternalError = 500` is excluded. [5](#0-4) 

### Impact Explanation
Each malformed request causes the server to fetch block headers and attempt (and fail) MMR proof generation without penalizing the sender. The peer can repeat this at will. The per-request cost is low (fast MMR bounds failure), so the practical impact is suboptimal: unnecessary work and misleading `InternalError` logs rather than a `MalformedProtocolMessage` ban.

### Likelihood Explanation
Any unprivileged P2P peer can craft this message. No PoW, key material, or privileged access is required. The condition (two main-chain blocks where one is taller than the other) is trivially satisfiable on any live chain.

### Recommendation
Add a height guard in `execute()` before appending to `positions`:

```rust
if header.number() > last_block.number() {
    return StatusCode::MalformedProtocolMessage
        .with_context("block height exceeds last_hash block");
}
```

This converts the path to a 4xx status, which triggers `should_ban()` and penalizes the peer. [2](#0-1) 

### Proof of Concept
Build a chain of height 10. Send `GetBlocksProof` with `last_hash = block[5].hash()` and `block_hashes = [block[8].hash()]`. Assert the response is `InternalError` and that `nc.ban_peer` is **not** called, confirming the missing validation and missing ban.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-95)
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
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L104-113)
```rust
        self.protocol
            .reply_proof::<packed::SendBlocksProofV1>(
                self.peer,
                self.nc,
                &last_block,
                positions,
                proved_items,
                missing_items,
            )
            .await
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
