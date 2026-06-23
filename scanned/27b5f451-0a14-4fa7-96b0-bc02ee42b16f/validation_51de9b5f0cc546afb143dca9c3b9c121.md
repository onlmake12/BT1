The code evidence confirms this is a real, concrete gap. Here is the analysis:

---

### Title
Missing Per-Peer Rate Limit in Light Client Protocol Server Enables CPU/I/O Exhaustion — (`util/light-client-protocol-server/src/lib.rs`)

### Summary

`LightClientProtocol::try_process` has no per-peer rate limiting, unlike the `Relayer` and `HolePunching` protocols which both use `governor::RateLimiter`. An unprivileged remote peer can flood the server with valid `GetBlocksProof` or `GetTransactionsProof` messages at maximum size (1000 items each), triggering unbounded sequential DB reads and MMR proof generation with no cooldown or ban.

### Finding Description

`LightClientProtocol` holds only a `Shared` field — no rate limiter: [1](#0-0) 

`try_process` dispatches directly to handlers with zero rate-limit check: [2](#0-1) 

`BAD_MESSAGE_BAN_TIME` and peer banning only fire on structurally malformed messages (parse failure → 4xx status). Valid proof requests return `StatusCode::OK` (200), which triggers neither a ban nor a warning: [3](#0-2) [4](#0-3) 

Each `GetBlocksProof` with 1000 hashes performs up to 1000 × 3 DB reads (header, uncles, extension) plus an MMR `gen_proof` call: [5](#0-4) [6](#0-5) 

Each `GetTransactionsProof` with 1000 hashes additionally computes a CBMT merkle proof and `calc_witnesses_root` per block: [7](#0-6) 

The item limits are 1000 for both message types: [8](#0-7) 

**Contrast with other protocols:** `Relayer::try_process` checks `rate_limiter.check_key(&(peer, message.item_id()))` before any processing and returns `StatusCode::TooManyRequests` on excess: [9](#0-8) 

`HolePunching` similarly has both a `rate_limiter` and `forward_rate_limiter`: [10](#0-9) 

The light client protocol has neither.

### Impact Explanation

Because `received` takes `&mut self`, the handler is exclusive — one peer's flood of max-size valid requests occupies the handler sequentially, starving all other light-client peers of responses. The impact is sustained throughput degradation for all peers sharing the same light-client server, matching the Low (501–2000) scope.

### Likelihood Explanation

The attacker needs only a standard P2P connection and the ability to send well-formed `GetBlocksProof` or `GetTransactionsProof` messages. No privilege, key, or hashpower is required. The gap is directly reachable from the public P2P interface.

### Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` to `LightClientProtocol` (mirroring `Relayer`) and check it at the top of `try_process`, returning an appropriate non-4xx status (to avoid banning legitimate slow clients) or a dedicated `TooManyRequests` status that does not trigger a ban but drops the request.

### Proof of Concept

1. Connect two peers A and B to the light-client server.
2. From peer A, send `GetBlocksProof` messages in a tight loop, each containing 1000 valid main-chain block hashes and a valid `last_hash`.
3. From peer B, send a single `GetBlocksProof` request and measure response latency.
4. Assert that peer B's response latency grows proportionally to peer A's flood rate, confirming monopolization of the handler.

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

**File:** util/light-client-protocol-server/src/lib.rs (L63-92)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L86-97)
```rust
            let merkle_proof = CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &txs_and_tx_indices
                    .iter()
                    .map(|(_, index)| *index as u32)
                    .collect::<Vec<_>>(),
            )
            .expect("build proof with verified inputs should be OK");
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```
