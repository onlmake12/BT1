Looking at the actual code, I need to trace the full execution path and check for any guards.

The code confirms the attack path. Let me verify the critical missing rate-limit comparison between `LightClientProtocol` and `Relayer`:

- `Relayer::try_process` (sync/src/relayer/mod.rs:89-123): explicit `RateLimiter` at 30 req/s per peer per message type
- `LightClientProtocol::try_process` (lib.rs:96-125): **no rate limiter at all**

The full execution chain is confirmed in code. Here is the assessment:

---

### Title
Unbounded MMR Proof Generation via `GetBlocksProof` with No Per-Peer Rate Limiting — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`, `util/light-client-protocol-server/src/lib.rs`)

### Summary
Any unprivileged peer can send a `GetBlocksProof` message with up to 1000 valid main-chain block hashes, forcing the server to perform O(1000 × log(N)) RocksDB reads from `COLUMN_CHAIN_ROOT_MMR` per request, with no rate limiting and no ban trigger for valid requests. The `LightClientProtocol` handler has no rate limiter, unlike the `Relayer` handler which explicitly caps at 30 req/s per peer.

### Finding Description

**Entry point:** Any peer connected on the light client P2P protocol sends a `LightClientMessage::GetBlocksProof`.

**Step 1 — `execute`** [1](#0-0) 

The only size guard is `block_hashes().len() > GET_BLOCKS_PROOF_LIMIT` (1000). A request with exactly 1000 hashes passes. [2](#0-1) 

**Step 2 — Per-hash DB reads:** For each of the up to 1000 found hashes, the handler calls `get_block_header`, `get_block_uncles`, and `get_block_extension` — up to 3,000 RocksDB reads. [3](#0-2) 

**Step 3 — `reply_proof`:** Constructs `chain_root_mmr(last_block.number() - 1)` and calls `mmr.gen_proof(items_positions)` with up to 1000 positions. [4](#0-3) 

The MMR store backend is `&Snapshot`, whose `get_elem` calls `self.store.get_header_digest(pos)` — a live RocksDB read from `COLUMN_CHAIN_ROOT_MMR` for every MMR node visited. [5](#0-4) 

For a chain of N blocks, `gen_proof` for k positions requires O(k × log(N)) node reads. At N = 10M and k = 1000: ~23,000 MMR reads + 3,000 block reads = ~26,000 RocksDB reads per single request.

**Step 4 — No rate limiting:** `LightClientProtocol::try_process` dispatches directly with no rate limiter. [6](#0-5) 

Compare to `Relayer`, which explicitly installs a `governor`-based rate limiter at 30 req/s per peer per message type before dispatching: [7](#0-6) 

**Step 5 — No ban for valid requests:** A well-formed request with 1000 valid hashes returns `Status::ok()`, which never triggers `should_ban()`. The peer is never disconnected or penalized. [8](#0-7) 

### Impact Explanation
A single attacker peer can continuously send max-size `GetBlocksProof` messages. Each request forces ~26,000 RocksDB random reads on a 10M-block chain. Multiple concurrent peers multiply this linearly. This saturates the node's storage I/O, causing latency spikes for all other operations (block sync, tx relay, RPC) that share the same RocksDB instance.

### Likelihood Explanation
The attack requires only a valid P2P connection to a node running the light client protocol. No PoW, no keys, no privileged access. The attacker needs only to know 1000 valid main-chain block hashes (trivially obtained from any block explorer or by syncing headers). The attack is repeatable indefinitely with no cooldown.

### Recommendation
Add a per-peer rate limiter to `LightClientProtocol`, mirroring the pattern already used in `Relayer`: [9](#0-8) 

Additionally, consider reducing `GET_BLOCKS_PROOF_LIMIT` or adding a cost-based admission check that accounts for chain length.

### Proof of Concept
1. Sync a CKB node to height N >> 1000.
2. Collect 1000 distinct main-chain block hashes (e.g., blocks 1, 10000, 20000, …, 10000000).
3. Connect as a light client peer and send `GetBlocksProof { last_hash: tip_hash, block_hashes: [h1..h1000] }` in a tight loop.
4. Monitor RocksDB read IOPS and node response latency — both will spike proportionally to the request rate, with no ban or throttle applied to the sender.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L33-40)
```rust
    pub(crate) async fn execute(self) -> Status {
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L81-91)
```rust
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L199-216)
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
                }
```

**File:** util/snapshot/src/lib.rs (L293-296)
```rust
impl MMRStore<HeaderDigest> for &Snapshot {
    fn get_elem(&self, pos: u64) -> MMRResult<Option<HeaderDigest>> {
        Ok(self.store.get_header_digest(pos))
    }
```

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
