All seven cited code references are confirmed against the actual repository. Every factual claim checks out:

- [1](#0-0)  — `LightClientProtocol` has only `shared: Shared`, no rate limiter field.
- [2](#0-1)  — `try_process()` dispatches directly with no rate-limit gate.
- [3](#0-2)  — `StatusCode` has no `TooManyRequests` variant.
- [4](#0-3)  — `Relayer` has `rate_limiter` checked before every dispatch.
- [5](#0-4)  — `HolePunching` has two keyed rate limiters.
- [6](#0-5)  — `reply_proof()` calls `mmr.get_root()` and `mmr.gen_proof()` on every valid message.
- [7](#0-6)  — `GET_BLOCKS_PROOF_LIMIT` caps hashes per message at 1000, no per-peer message-rate cap exists.

---

Audit Report

## Title
Missing Per-Peer Rate Limit on `GetBlocksProof` Enables Unbounded MMR Proof Generation — (`util/light-client-protocol-server/src/lib.rs`)

## Summary
`LightClientProtocol` has no per-peer message rate limiter. Any connected peer can send an unlimited stream of `GetBlocksProof` messages, each triggering expensive MMR `get_root()` and `gen_proof()` operations backed by RocksDB reads, with no throttling or back-pressure. This contrasts directly with `Relayer` and `HolePunching`, which both enforce explicit per-peer rate limits for exactly this reason.

## Finding Description
`LightClientProtocol` contains only a `shared: Shared` field — no rate limiter is present: [8](#0-7) 

`try_process()` dispatches directly to `GetBlocksProofProcess::execute()` with no rate-limit gate before any message type: [2](#0-1) 

`execute()` only validates message structure (empty / too-many / duplicate hashes). A message with exactly 1 valid main-chain hash passes all checks and proceeds to the expensive path: [9](#0-8) 

`reply_proof()` then calls `chain_root_mmr(last_block.number() - 1).get_root()` and `mmr.gen_proof(positions)` — both O(log N) in chain height, involving RocksDB reads and hash computations: [6](#0-5) 

By contrast, `Relayer` explicitly rate-limits every message type at 30 req/s per peer before dispatch: [4](#0-3) 

`HolePunching` similarly has two keyed rate limiters: [5](#0-4) 

The `StatusCode` enum has no `TooManyRequests` variant, confirming rate limiting was never implemented for this protocol: [3](#0-2) 

## Impact Explanation
A single unprivileged peer can saturate the server's CPU and RocksDB I/O by sending a continuous stream of minimal (1-hash) `GetBlocksProof` messages. On a mainnet chain of height H, each message costs O(log H) MMR hash computations plus multiple DB reads (`get_block`, `get_block_header`, `get_block_uncles`, `get_block_extension`). With no rate limit, this degrades or blocks proof-serving for all other light-client peers sharing the same server. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**, as the light-client server is part of the CKB network infrastructure and the attack cost is a single P2P connection sending well-formed messages.

## Likelihood Explanation
The attack requires only a standard P2P connection to a node with the light-client protocol enabled. No credentials, proof-of-work, or special state are needed. The attacker sends valid, well-formed messages that pass all structural checks. The gap is directly visible by comparing `LightClientProtocol` to `Relayer` and `HolePunching`, which both have explicit rate limiters for exactly this reason. The attack is repeatable and requires no victim interaction.

## Recommendation
Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` (mirroring `Relayer`'s pattern) and check it at the top of `try_process()` before dispatching any message type. A limit of 30 req/s per peer per message type (matching `Relayer`'s quota) is a reasonable starting point. Also add a `TooManyRequests` variant to `StatusCode` in `status.rs`.

## Proof of Concept
1. Connect to a CKB node with light-client protocol enabled.
2. Obtain any 1 valid main-chain block hash (e.g., from the genesis block).
3. Send 10,000 `GetBlocksProof` messages in rapid succession, each containing that single hash and a valid `last_hash`.
4. Simultaneously connect a second peer and send a single `GetBlocksProof` request; measure response latency.
5. Expected result: the second peer's response is significantly delayed due to the server being occupied processing the flood from the first peer, with no ban or throttle applied to the attacker.

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

**File:** util/light-client-protocol-server/src/lib.rs (L199-217)
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
            };
```

**File:** util/light-client-protocol-server/src/status.rs (L14-38)
```rust
#[repr(u16)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StatusCode {
    /// OK
    OK = 200,

    /// Malformed protocol message.
    MalformedProtocolMessage = 400,
    /// Unexpected light-client protocol message.
    UnexpectedProtocolMessage = 401,

    /// The request data is incorrect.
    InvalidRequest = 410,
    /// The last block sent from client is invalid.
    InvalidLastBlock = 411,
    /// At least one unconfirmed block sent from client is invalid.
    InvalidUnconfirmedBlock = 412,
    /// The difficulty boundary is not in the provided block range.
    InvaildDifficultyBoundary = 413,

    /// Throws an internal error.
    InternalError = 500,
    /// Throws an error from the network.
    Network = 501,
}
```

**File:** sync/src/relayer/mod.rs (L81-123)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L33-50)
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
```
