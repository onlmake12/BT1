Audit Report

## Title
Missing Per-Peer Rate Limiter in `LightClientProtocol` Enables Amplified DB and MMR Load via `GetBlocksProof` — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
`LightClientProtocol` contains no rate limiter of any kind, unlike `Relayer` and `HolePunching` which both enforce a 30 req/s per-peer-per-message-type limit via `governor::RateLimiter`. An unprivileged remote peer can send a continuous stream of `GetBlocksProof` messages each containing up to 1000 valid on-chain block hashes, forcing the server to perform up to 3000 synchronous RocksDB reads plus an O(N log N) MMR proof generation over 1000 positions per message, with no throttle of any kind.

## Finding Description
`LightClientProtocol` is defined with only a `shared: Shared` field — no rate limiter field exists. [1](#0-0) 

`LightClientProtocol::received` parses the message and calls `try_process`, which dispatches directly to `GetBlocksProofProcess::execute` with zero rate-limiting logic at any point in the call chain. [2](#0-1) 

`GetBlocksProofProcess::execute` accepts up to `GET_BLOCKS_PROOF_LIMIT = 1000` block hashes per message (the only guard is a rejection if `len > 1000`). [3](#0-2) [4](#0-3) 

For each hash found on the main chain, the handler performs three synchronous DB reads: `get_block_header`, `get_block_uncles`, and `get_block_extension`. [5](#0-4) 

All collected positions are then passed to `reply_proof`, which calls `mmr.gen_proof(items_positions)` — an O(N log N) MMR computation over up to 1000 positions — unconditionally when positions are non-empty. [6](#0-5) 

A grep for `rate_limiter`, `RateLimiter`, and `governor` across the entire `util/light-client-protocol-server/` tree returns zero matches, confirming no throttling exists at any level in this handler.

By contrast, `Relayer` explicitly constructs a `RateLimiter<(PeerIndex, u32)>` at 30 req/s keyed by peer and message type: [7](#0-6) 

And enforces it at the top of `try_process`: [8](#0-7) 

`HolePunching` does the same: [9](#0-8) 

The omission in `LightClientProtocol` is not a design choice — it is an unintentional gap relative to the established pattern in the codebase.

## Impact Explanation
A single attacker peer can saturate the node's RocksDB read I/O and async executor threads with max-cost requests at line rate. With multiple peers (each allowed by the P2P connection manager), the amplification multiplies. The DB snapshot reads and MMR proof generation compete directly with the chain processing loop's own DB access and block validation work. Targeting multiple full nodes simultaneously with minimal attacker cost fits the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
The exploit requires only a valid P2P connection and knowledge of 1000 on-chain block hashes, both trivially obtained from any block explorer or by syncing. No PoW, no key, no privileged role is required. The attacker sends well-formed, protocol-valid messages at maximum rate. The server has no mechanism to detect or throttle this. The light client protocol is a production feature enabled on full nodes that serve light clients.

## Recommendation
Add a per-peer rate limiter to `LightClientProtocol`, mirroring the pattern already used in `Relayer` and `HolePunching`:

```rust
// In LightClientProtocol struct:
rate_limiter: RateLimiter<(PeerIndex, u32)>,

// In LightClientProtocol::new:
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);

// At the top of try_process:
if self.rate_limiter.check_key(&(peer_index, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.into();
}
```

Additionally, consider a tighter per-message limit for `GetBlocksProof` specifically (e.g., 100 hashes) given its high per-item DB cost relative to other message types.

## Proof of Concept
1. Connect to a CKB full node with the light client protocol enabled.
2. Obtain 1000 valid on-chain block hashes and the current tip hash (from any block explorer or by syncing).
3. In a tight loop, send `GetBlocksProof` messages each containing all 1000 hashes and the valid `last_hash`.
4. Observe: the server performs 3000 DB reads + `mmr.gen_proof(1000)` per iteration with no throttle.
5. Measure: concurrent block validation throughput (via `ckb_chain_process_block_duration` metrics) degrades below baseline under sustained load from a single peer.
6. Scale: repeat with multiple simultaneous peers to amplify the effect proportionally.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-35)
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-125)
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
}

impl LightClientProtocol {
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

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
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
