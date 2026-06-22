Let me check a few more details about the light client protocol's deployment and any P2P-level mitigations.

All the evidence needed is now in hand. Here is the assessment:

---

### Title
Missing Per-Peer Rate Limit on `GetBlocksProof` Enables Amplified DB/MMR DoS — (`util/light-client-protocol-server/src/lib.rs`, `src/components/get_blocks_proof.rs`)

### Summary

`LightClientProtocol` has no rate limiter at any layer. A single connected peer can continuously send `GetBlocksProof` messages each containing up to 1,000 valid on-chain block hashes, forcing the server to perform up to 3,000 RocksDB reads plus an MMR proof generation over 1,000 positions per message, with no throttling. This is a concrete, measurable DoS amplification path against a protocol that is **enabled by default**.

### Finding Description

**`LightClientProtocol` struct — no rate limiter field:**

`LightClientProtocol` holds only `shared: Shared`. [1](#0-0) 

**`try_process` — no rate check before dispatch:**

`LightClientProtocol::try_process` dispatches directly to handlers with zero rate-limit gating. [2](#0-1) 

**Contrast with `Relayer` — explicit 30 req/s keyed rate limiter:** [3](#0-2) 

**Contrast with `HolePunching` — two keyed rate limiters (30 req/s and 1 req/s):** [4](#0-3) 

**`GET_BLOCKS_PROOF_LIMIT = 1000` — the maximum amplification factor:** [5](#0-4) 

**`GetBlocksProofProcess::execute` — 3 DB reads per found block hash:**

For each of up to 1,000 found hashes: `get_block_header` + `get_block_uncles` + `get_block_extension`. [6](#0-5) 

**`reply_proof` — `mmr.gen_proof(1000 positions)` per message:** [7](#0-6) 

**`LightClient` is in the default `support_protocols` list:** [8](#0-7) [9](#0-8) 

**`LightClient` is registered in the launcher unconditionally when configured (default):** [10](#0-9) 

### Impact Explanation

Each `GetBlocksProof` message with 1,000 valid block hashes triggers:
- Up to 3,000 synchronous RocksDB reads (header + uncles + extension per block)
- One `mmr.gen_proof(Vec<u64>)` call over 1,000 leaf positions

`LightClientProtocol` shares the same `Shared` state (and therefore the same RocksDB instance) as the main chain processing loop. Sustained high-rate requests from even a single peer saturate the DB I/O and CPU available to block validation and chain tip advancement. The max frame length for `LightClient` is 2 MB, easily accommodating 1,000 × 32-byte hashes. [11](#0-10) 

### Likelihood Explanation

- The protocol is **on by default** in `ckb.toml` and `default_support_all_protocols()`.
- Any peer that can establish a TCP connection can open the `/ckb/lightclient` sub-protocol (protocol ID 120) and send well-formed `GetBlocksProof` messages.
- No PoW, no stake, no privileged role is required.
- The attacker only needs to know 1,000 valid block hashes from the canonical chain (trivially obtained by syncing headers first via the `Sync` protocol).
- The attack is local-testable: spin up a node, connect a crafted peer, send max-size `GetBlocksProof` at line rate, and measure block validation throughput degradation.

### Recommendation

Add a per-peer, per-message-type keyed rate limiter to `LightClientProtocol`, mirroring the pattern already used in `Relayer` and `HolePunching`:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

Apply the check at the top of `try_process`, before dispatching to any handler. A quota of 1–5 req/s per peer per message type is sufficient for legitimate light-client usage while eliminating the amplification window.

### Proof of Concept

1. Sync a full node to height H, recording 1,000 canonical block hashes `h_1..h_1000`.
2. Connect a crafted peer that opens `SupportProtocols::LightClient` (protocol ID 120).
3. In a tight loop, send `GetBlocksProof { last_hash: tip_hash, block_hashes: [h_1..h_1000] }`.
4. Concurrently measure the full node's block validation throughput (blocks/s) and RocksDB read latency.
5. Assert that throughput does not degrade below an acceptable threshold — it will, because each message forces 3,000 DB reads + `mmr.gen_proof(1000)` with no throttle.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
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

**File:** sync/src/relayer/mod.rs (L88-123)
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-107)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}

#[async_trait]
impl ServiceProtocol for HolePunching {
    async fn init(&mut self, context: &mut ProtocolContext) {
        context
            .set_service_notify(context.proto_id, CHECK_INTERVAL, CHECK_TOKEN)
            .await
            .expect("set discovery notify fail")
    }

    async fn connected(&mut self, context: ProtocolContextMutRef<'_>, version: &str) {
        self.network_state.with_peer_registry_mut(|reg| {
            reg.get_peer_mut(context.session.id).map(|peer| {
                peer.protocols.insert(context.proto_id, version.to_owned());
            })
        });
    }

    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
    }

    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: bytes::Bytes) {
        let session_id = context.session.id;
        trace!("HolePunching.received session={}", session_id);

        let msg = match packed::HolePunchingMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "HolePunching.received a malformed message from {}",
                    session_id
                );
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    session_id,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();

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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
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

**File:** util/launcher/src/lib.rs (L467-476)
```rust
        if support_protocols.contains(&SupportProtocol::LightClient) {
            let light_client = LightClientProtocol::new(shared.clone());
            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::LightClient,
                Box::new(light_client),
                Arc::clone(&network_state),
            ));
        } else {
            flags.remove(Flags::LIGHT_CLIENT);
        }
```

**File:** network/src/protocols/support_protocols.rs (L133-133)
```rust
            SupportProtocols::LightClient => 2 * 1024 * 1024, // 2 MB
```
