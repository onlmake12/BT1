Let me check whether the light client protocol is enabled by default and if there are any connection-level limits.

### Title
Missing Per-Peer Rate Limiter in `LightClientProtocol` Allows Unbounded I/O Exhaustion via `GetBlocksProof` — (`util/light-client-protocol-server/src/lib.rs`, `src/components/get_blocks_proof.rs`)

---

### Summary

`LightClientProtocol` (protocol ID 120, enabled by default) has no per-peer rate limiter. Any unprivileged P2P peer can send a continuous stream of valid `GetBlocksProof` messages each requesting up to 1000 block hashes, triggering up to 3000 synchronous DB reads plus an `mmr.gen_proof(1000 positions)` call per message, with zero throttle or ban. This contrasts directly with `Relayer` and `HolePunching`, which both enforce a `governor::RateLimiter` keyed by `(PeerIndex, message_type)` before any processing.

---

### Finding Description

**`LightClientProtocol` struct has no rate limiter field:** [1](#0-0) 

The `received` → `try_process` path performs no rate-limit check before dispatching to `GetBlocksProofProcess::execute`: [2](#0-1) [3](#0-2) 

`GetBlocksProofProcess::execute` only validates message structure (empty, >1000, duplicate hashes), then performs up to 3 DB reads per block hash (`get_block_header`, `get_block_uncles`, `get_block_extension`) for all 1000 found blocks: [4](#0-3) 

It then calls `reply_proof`, which invokes `mmr.gen_proof(items_positions)` with up to 1000 positions: [5](#0-4) 

**Contrast with `Relayer`**, which has an explicit `governor::RateLimiter<(PeerIndex, u32)>` and checks it before any message processing (30 req/sec per peer/message-type): [6](#0-5) 

**`HolePunching` similarly** has both a `rate_limiter` and `forward_rate_limiter` checked in `received`: [7](#0-6) 

**The LightClient protocol is enabled by default** in `ckb.toml` and `default_support_all_protocols`: [8](#0-7) [9](#0-8) 

Protocol ID and max frame size confirmed: [10](#0-9) [11](#0-10) 

---

### Impact Explanation

Each `GetBlocksProof(1000 valid hashes)` message causes:
- Up to 3000 synchronous RocksDB reads (`get_block_header` + `get_block_uncles` + `get_block_extension` per hash)
- One `mmr.gen_proof(1000 positions)` call (O(N log N) MMR traversal)

A single attacker peer sending these in a tight loop can saturate the full node's disk I/O and CPU, degrading or blocking service for honest peers and miners. The attacker cost is negligible: connect as a P2P peer, obtain 1000 valid block hashes from the public chain, loop.

---

### Likelihood Explanation

The LightClient protocol is on by default. Any peer that can establish a TCP connection can open protocol 120 and send `GetBlocksProof` messages. No PoW, stake, or privilege is required. The attack is trivially scriptable and locally testable.

---

### Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` (mirroring `Relayer::rate_limiter`) and check it at the top of `received` or `try_process` before dispatching to any handler. A quota of 30 req/sec per (peer, message_type) — consistent with the Relayer — would bound the per-peer DB load to a manageable level.

---

### Proof of Concept

1. Start a CKB full node with default config (LightClient enabled).
2. Mine or sync a chain of ≥1000 blocks.
3. From a separate process, open a P2P connection on protocol 120 (`/ckb/lightclient`).
4. Collect 1000 valid main-chain block hashes.
5. In a tight loop, send `GetBlocksProof { last_hash: tip_hash, block_hashes: [1000 hashes] }`.
6. Monitor the full node's disk I/O (`iostat`) and CPU. Assert that honest sync peers experience degraded throughput.

The node will process each message fully — 3000 DB reads + MMR proof — with no throttle, ban, or backpressure applied to the sending peer.

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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L33-95)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.block_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no block");
        }

        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }

        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendBlocksProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");

        let block_hashes: Vec<_> = self
            .message
            .block_hashes()
            .to_entity()
            .into_iter()
            .collect();

        let mut uniq = HashSet::new();
        if !block_hashes
            .iter()
            .chain([last_block_hash].iter())
            .all(|hash| uniq.insert(hash))
        {
            return StatusCode::MalformedProtocolMessage
                .with_context("duplicate block hash exists");
        }

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

**File:** sync/src/relayer/mod.rs (L63-123)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
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

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/app-config/src/configs/network.rs (L236-250)
```rust
pub fn default_support_all_protocols() -> Vec<SupportProtocol> {
    vec![
        SupportProtocol::Ping,
        SupportProtocol::Discovery,
        SupportProtocol::Identify,
        SupportProtocol::Feeler,
        SupportProtocol::DisconnectMessage,
        SupportProtocol::Sync,
        SupportProtocol::Relay,
        SupportProtocol::Time,
        SupportProtocol::Alert,
        SupportProtocol::LightClient,
        SupportProtocol::Filter,
        SupportProtocol::HolePunching,
    ]
```

**File:** network/src/protocols/support_protocols.rs (L72-73)
```rust
            SupportProtocols::LightClient => 120,
            SupportProtocols::Filter => 121,
```

**File:** network/src/protocols/support_protocols.rs (L133-133)
```rust
            SupportProtocols::LightClient => 2 * 1024 * 1024, // 2 MB
```
