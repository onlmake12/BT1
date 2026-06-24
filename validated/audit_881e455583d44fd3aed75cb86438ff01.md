Audit Report

## Title
Missing Per-Peer Rate Limiter in `LightClientProtocol` Allows Unbounded I/O Exhaustion via `GetBlocksProof` — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary

`LightClientProtocol` (protocol ID 120, enabled by default) contains no per-peer rate limiter. Any unprivileged P2P peer can send a continuous stream of valid `GetBlocksProof` messages, each requesting up to 1000 block hashes, triggering up to 3000 synchronous RocksDB reads plus an `mmr.gen_proof(1000 positions)` call per message with zero throttle or ban. This directly contrasts with `Relayer` and `HolePunching`, both of which enforce a `governor::RateLimiter` keyed by `(PeerIndex, message_type)` before any processing.

## Finding Description

**Root cause — no rate limiter field in `LightClientProtocol`:**

`LightClientProtocol` holds only a `Shared` field with no rate-limiting state:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
}
``` [1](#0-0) 

**No rate-limit check in `received` or `try_process`:**

The `received` handler parses the message and immediately calls `try_process`, which dispatches directly to `GetBlocksProofProcess::execute` with no quota check: [2](#0-1) [3](#0-2) 

**`GetBlocksProofProcess::execute` performs up to 3000 DB reads per message:**

The only guards are structural (empty list, >1000 hashes, duplicates). For each of up to 1000 found block hashes, the handler calls `get_block_header`, `get_block_uncles`, and `get_block_extension` — three synchronous RocksDB reads: [4](#0-3) 

The limit constant is confirmed at 1000: [5](#0-4) 

**`reply_proof` then calls `mmr.gen_proof` with up to 1000 positions (O(N log N)):** [6](#0-5) 

**Contrast: `Relayer` enforces 30 req/sec per `(PeerIndex, message_type)` before any dispatch:** [7](#0-6) [8](#0-7) [9](#0-8) 

**Contrast: `HolePunching` also checks its rate limiter before any processing:** [10](#0-9) 

**LightClient is enabled by default** in `default_support_all_protocols`: [11](#0-10) 

**Protocol ID 120, max frame 2 MB:** [12](#0-11) [13](#0-12) 

The same unbounded pattern also applies to `GetTransactionsProof` (up to 1000 tx hashes, per-block full reads + CBMT proof + MMR proof) and `GetLastStateProof`, all dispatched through the same unguarded `try_process`. [14](#0-13) 

## Impact Explanation

A single attacker peer sending `GetBlocksProof(1000 valid hashes)` in a tight loop causes up to 3000 synchronous RocksDB reads and one O(N log N) MMR traversal per message, with no backpressure or ban applied. This can saturate the full node's disk I/O and CPU, degrading or blocking service for honest sync peers and miners. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (10001–15000 points). The attacker cost is negligible: establish one TCP connection, obtain 1000 valid block hashes from the public chain, loop.

## Likelihood Explanation

The LightClient protocol is on by default. Any peer that can establish a TCP connection can open protocol 120 (`/ckb/lightclient`) and send `GetBlocksProof` messages. No proof-of-work, stake, or privilege is required. The attack is trivially scriptable, locally testable, and repeatable indefinitely since no ban is triggered by well-formed messages.

## Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, mirroring the pattern in `Relayer`:

```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

pub struct LightClientProtocol {
    pub shared: Shared,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

Check it at the top of `try_process` (or `received`) before dispatching to any handler, using the same 30 req/sec per `(peer, message_type)` quota as `Relayer`. This bounds per-peer DB load to a manageable level and is consistent with the existing defense-in-depth pattern already applied to `Relayer` and `HolePunching`.

## Proof of Concept

1. Start a CKB full node with default config (`LightClient` enabled via `default_support_all_protocols`).
2. Sync or mine a chain of ≥1000 blocks.
3. From a separate process, open a P2P connection on protocol 120 (`/ckb/lightclient`).
4. Collect 1000 distinct valid main-chain block hashes.
5. In a tight loop, send `GetBlocksProof { last_hash: <tip_hash>, block_hashes: [1000 hashes] }`.
6. Monitor the full node's disk I/O (`iostat -x 1`) and CPU (`top`). Observe that each message is fully processed — 3000 DB reads + MMR proof — with no throttle, ban, or backpressure applied to the sending peer.
7. Confirm that honest sync peers experience degraded throughput during the attack.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
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

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
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

**File:** util/app-config/src/configs/network.rs (L236-251)
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
}
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L32-39)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```
