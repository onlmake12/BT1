### Title
Unbounded Per-Request Resource Exhaustion in `GetBlocksProofProcess::execute` — No Rate Limit on 3000 DB Reads + O(N log M) MMR Proof Generation per Valid Request - (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary

Any unprivileged P2P peer can send a structurally valid `GetBlocksProof` message containing exactly `GET_BLOCKS_PROOF_LIMIT` (1000) distinct main-chain block hashes. The server performs 3 DB reads per hash (header, uncles, extension) plus an O(1000 × log(chain_length)) MMR proof generation, all without any per-peer rate limit. Because the request is valid, the server returns `Status::ok()` and the peer is never banned, allowing indefinite repetition.

### Finding Description

`GET_BLOCKS_PROOF_LIMIT` is set to 1000 with no accompanying rate-limiting mechanism. [1](#0-0) 

The `execute` function enforces only structural validity (non-empty, ≤ 1000, no duplicates). A request satisfying these checks is never rejected or penalized: [2](#0-1) 

For every hash that passes `is_main_chain`, three synchronous DB reads are issued unconditionally: [3](#0-2) 

All 1000 positions are then passed to `mmr.gen_proof`, which reads O(1000 × log(chain_length)) MMR nodes from storage: [4](#0-3) 

A valid request returns `Status::ok()` (code 200). The banning logic only fires on 4xx codes: [5](#0-4) 

No rate-limiting, quota, or token-bucket mechanism exists anywhere in the light-client-protocol-server:



### Impact Explanation

A single peer sending a continuous stream of max-size `GetBlocksProof` messages forces:
- Up to **3,000 DB reads per request** (1000 hashes × header + uncles + extension)
- **O(1000 × log N) MMR node reads per request** for proof generation

At even modest request rates this saturates the RocksDB I/O budget and CPU, starving the consensus reactor (block relay, IBD) of the DB access it needs. The node degrades or stalls without the attacker ever being banned.

### Likelihood Explanation

The attack requires only a valid P2P connection and knowledge of 1000 main-chain block hashes (trivially obtained from any block explorer or by syncing headers). No PoW, no key, no privileged role. The attacker can open multiple connections to multiply the effect.

### Recommendation

1. **Per-peer rate limit**: enforce a maximum number of `GetBlocksProof` requests per peer per time window (e.g., 1 request/second per peer) using a token-bucket or leaky-bucket counter stored in peer state.
2. **Reduce the effective limit**: lower `GET_BLOCKS_PROOF_LIMIT` or charge a cost proportional to the number of items before doing any DB work.
3. **Punish excessive requests**: return a 4xx `StatusCode` (triggering `BAD_MESSAGE_BAN_TIME`) when a peer exceeds the rate limit, so the banning logic in `lib.rs` fires. [6](#0-5) 

### Proof of Concept

```
1. Connect to a CKB node with the light-client protocol enabled.
2. Obtain 1000 distinct main-chain block hashes (e.g., blocks 1–1000).
3. In a tight loop, send GetBlocksProof{last_hash: tip, block_hashes: [h1..h1000]}.
4. Observe: RocksDB read IOPS spike to millions/second; block relay latency increases;
   the node's consensus participation degrades.
5. The peer is never banned because every response is Status::ok().
```

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L34-40)
```rust
        if self.message.block_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no block");
        }

        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
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

**File:** util/light-client-protocol-server/src/lib.rs (L81-86)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-212)
```rust
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
```

**File:** util/light-client-protocol-server/src/status.rs (L95-102)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
    }
```
