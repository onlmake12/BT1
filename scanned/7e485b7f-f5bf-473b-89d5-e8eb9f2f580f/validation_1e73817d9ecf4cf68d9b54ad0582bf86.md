Based on my code review, here is my analysis:

**Code facts confirmed:**

1. `GET_BLOCKS_PROOF_LIMIT = 1000` — the only per-request guard. [1](#0-0) 

2. The `execute()` function rejects only if `len > 1000` or if there are duplicates. For a valid request with ≤1000 unique main-chain hashes, it proceeds unconditionally. [2](#0-1) 

3. For every hash that passes `is_main_chain()`, the server performs exactly 3 synchronous DB reads: `get_block_header`, `get_block_uncles`, `get_block_extension`. [3](#0-2) 

4. `reply_proof` then calls `mmr.get_root()` and `mmr.gen_proof(items_positions)` with up to 1000 positions — O(N log M) MMR node reads. [4](#0-3) 

5. There is **no rate limiting, no per-peer request counter, no backpressure, and no ban** for well-formed valid requests. The only ban path is for unparseable messages or statuses where `should_ban()` returns `Some`. [5](#0-4) 

6. The `received` handler is `async fn` dispatching to `try_process` — if the network layer spawns tasks per message (common in CKB's tokio-based network), multiple requests from the same peer can be in-flight concurrently. [6](#0-5) 

---

### Title
Unbounded per-request DB and MMR amplification in `GetBlocksProofProcess::execute` with no per-peer rate limiting — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary
A single unprivileged remote peer can send repeated `GetBlocksProof` messages each containing 1000 valid main-chain block hashes, causing the server to perform up to 3000 DB reads and O(1000 × log(chain_length)) MMR node reads per message, with no rate limiting or ban applied for valid requests.

### Finding Description
`GET_BLOCKS_PROOF_LIMIT = 1000` is the only guard. Once a message passes the size and duplicate checks, the server iterates all `found` hashes and unconditionally calls `get_block_header`, `get_block_uncles`, and `get_block_extension` for each — 3000 DB reads for a maximal request. It then calls `mmr.gen_proof(1000 positions)` which traverses O(1000 × log N) MMR nodes. No per-peer request rate limit, no concurrent-request cap, and no ban-on-valid-request mechanism exists anywhere in the handler or the protocol dispatch loop. [7](#0-6) [8](#0-7) 

### Impact Explanation
The amplification ratio is approximately 3000 DB reads + O(1000 log N) MMR reads per single P2P message. A peer sending even a modest stream of such messages can saturate the node's RocksDB I/O and CPU, starving consensus relay processing. The impact is resource exhaustion degrading or halting the node's participation in block propagation.

### Likelihood Explanation
The exploit requires only a valid P2P connection and knowledge of 1000 main-chain block hashes (trivially obtained by syncing headers). No PoW, no key, no privilege. The path is fully reachable from an unprivileged remote peer.

### Recommendation
- Introduce a per-peer sliding-window rate limit on `GetBlocksProof` messages (e.g., N requests per second per peer).
- Consider reducing `GET_BLOCKS_PROOF_LIMIT` or requiring a proof-of-work/stake for large requests.
- Apply a short ban or exponential backoff when a peer sends requests at a rate exceeding the limit.

### Proof of Concept
1. Connect to a CKB node running the light-client protocol server.
2. Sync enough headers to collect 1000 distinct main-chain block hashes at heights ≤ tip.
3. Send 100 concurrent `GetBlocksProof` messages each containing all 1000 hashes.
4. Observe: ~300,000 DB reads triggered immediately; MMR proof generation for 100,000 positions; relay message latency spikes measurably; node's block-propagation participation degrades.

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

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

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L195-218)
```rust
        let (parent_chain_root, proof) = if last_block.is_genesis() {
            (Default::default(), Default::default())
        } else {
            let snapshot = self.shared.snapshot();
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
                }
            };
            (parent_chain_root, proof)
```
