Audit Report

## Title
Unbounded `GetBlocksProof` Request Amplification in `LightClientProtocol` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
`LightClientProtocol` contains no rate limiter of any kind. Any peer speaking the `/ckb/lightclient` protocol can continuously send `GetBlocksProof` messages each carrying up to `GET_BLOCKS_PROOF_LIMIT = 1000` valid block hashes, forcing the server to execute up to 3,000 synchronous RocksDB reads plus an `mmr.gen_proof(1000 positions)` call per message with zero throttling. The light-client protocol is enabled by default in the production configuration, exposing every standard CKB full node.

## Finding Description
**Root cause:** `LightClientProtocol` carries only `pub shared: Shared` — no `rate_limiter` field exists anywhere in the crate, confirmed by code inspection and grep. [1](#0-0) 

`received` calls `try_process` with no rate-limit check before dispatch: [2](#0-1) 

`try_process` dispatches directly to `GetBlocksProofProcess::execute` with no interposed throttle: [3](#0-2) 

**Work per message in `GetBlocksProofProcess::execute`:**
- Size guard rejects only if `len > 1000`; a message at exactly 1,000 hashes passes: [4](#0-3) 
- For each of up to 1,000 found hashes: `get_block_header` + `get_block_uncles` + `get_block_extension` = **3,000 synchronous RocksDB reads**: [5](#0-4) 
- `reply_proof` → `mmr.gen_proof(items_positions)` with up to 1,000 positions — O(N log N) in MMR size: [6](#0-5) 

**Contrast with rate-limited sibling protocols:**

`Relayer` enforces 30 req/s per `(PeerIndex, message_type)` before any handler runs: [7](#0-6) 

`HolePunching` enforces the same keyed rate limiter at the top of `received`: [8](#0-7) 

`LightClientProtocol` is the only request-serving protocol with no equivalent guard.

**Default deployment:** `LightClient` is included in `support_protocols` in the production `ckb.toml`, so every standard CKB full node is exposed: [9](#0-8) 

The limit constant confirming maximum per-message work: [10](#0-9) 

## Impact Explanation
Each `GetBlocksProof` message at the 1,000-hash limit causes 3,000 synchronous RocksDB point-reads and one O(N log N) MMR proof traversal, all executed inside the async tokio handler, directly competing with block validation reads and writes on the same RocksDB instance. Sustained flooding from a single peer degrades block-validation throughput; with `max_peers = 125`, coordinated peers multiply the effect linearly, making it feasible to stall a node's chain-tip tracking entirely. [11](#0-10) 

This maps to **High (10,001–15,000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — and additionally to **High: Vulnerabilities which could easily crash a CKB node** given that I/O saturation can cause the node to fall behind consensus and become effectively non-functional.

## Likelihood Explanation
- No PoW, stake, or privileged role required — any peer completing the TCP handshake and speaking the light-client protocol can trigger this.
- The attacker needs only 1,000 valid main-chain block hashes (trivially obtained from any block explorer) and the current tip hash as `last_hash`.
- The attack is repeatable in a tight loop with a single connection and is locally reproducible in a test environment.
- The protocol is enabled by default on every production CKB full node.

## Recommendation
Add a per-`(PeerIndex, message_type)` `governor::RateLimiter` field to `LightClientProtocol`, mirroring the pattern in `Relayer` (`sync/src/relayer/mod.rs`) and `HolePunching` (`network/src/protocols/hole_punching/mod.rs`). A quota of 1–5 req/s per peer per message type is sufficient for legitimate light-client use. Additionally, move the synchronous RocksDB reads in `GetBlocksProofProcess::execute` to a `spawn_blocking` task to avoid blocking the async executor under load. [12](#0-11) 

## Proof of Concept
```
1. Sync a CKB node to obtain 1,000 valid main-chain block hashes H_1..H_1000
   (e.g., via RPC get_block_by_number for blocks 1..1000).
2. Connect to the target node on /ckb/lightclient (protocol ID 120).
3. In a tight loop, send:
     LightClientMessage::GetBlocksProof {
         last_hash: <current tip hash>,
         block_hashes: [H_1, ..., H_1000]
     }
4. Monitor RocksDB read IOPS on the target node (via ckb metrics or
   rocksdb statistics) and concurrent block-validation throughput.
5. Assert: block-validation throughput degrades measurably while the
   flood is active; the node falls behind the chain tip.
```

The absence of any rate-limit check between `received` and `GetBlocksProofProcess::execute` means step 3 is processed at the full rate the network and the server's async queue allow, with no server-side throttle. [13](#0-12)

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-36)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}

impl LightClientProtocol {
    /// Create a new light client protocol handler.
    pub fn new(shared: Shared) -> Self {
        Self { shared }
    }
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

**File:** util/light-client-protocol-server/src/lib.rs (L113-117)
```rust
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
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
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```
