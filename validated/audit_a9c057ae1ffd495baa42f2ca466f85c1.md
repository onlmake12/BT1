Audit Report

## Title
Unbounded Per-Message RocksDB + MMR Amplification DoS in Light Client `GetBlocksProof` Handler — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary

`LightClientProtocol` has no per-peer rate limiter. `GetBlocksProofProcess::execute()` accepts up to 1 000 valid block hashes per message, triggering 4 000 synchronous RocksDB reads and one 1 000-position MMR proof computation per message. Because well-formed requests always return `Status::ok()`, no ban is ever issued, allowing a single unauthenticated peer to saturate server I/O and CPU indefinitely.

## Finding Description

`GET_BLOCKS_PROOF_LIMIT` is set to 1 000. [1](#0-0) 

`execute()` rejects only messages with **strictly more than** 1 000 hashes; a message with exactly 1 000 valid hashes passes all guards and enters the processing loop. [2](#0-1) 

For every hash in `found`, the handler performs three synchronous RocksDB reads (`get_block_header`, `get_block_uncles`, `get_block_extension`) plus the earlier `is_main_chain` partition scan — 4 reads × 1 000 hashes = 4 000 reads per message. [3](#0-2) 

`reply_proof()` then calls `mmr.gen_proof(items_positions)` with up to 1 000 leaf positions, a CPU-bound MMR tree traversal. [4](#0-3) 

`LightClientProtocol` carries no `rate_limiter` field and `try_process()` performs no rate check before dispatching any handler. [5](#0-4) [6](#0-5) 

The ban logic fires only on 4xx status codes. A successful request returns `StatusCode::OK` (200), which never triggers a ban. [7](#0-6) 

By contrast, the `Relayer` protocol has an identical dispatch structure but explicitly rate-limits every non-PoW message per `(peer, item_id)` at 30 req/s before processing, and returns `StatusCode::TooManyRequests` (a 4xx code) on breach. [8](#0-7) [9](#0-8) 

The light client protocol server has no equivalent guard. `StatusCode` in the light client module does not even define a `TooManyRequests` variant. [10](#0-9) 

## Impact Explanation

Each max-size `GetBlocksProof` message forces:
- 1 000 × `is_main_chain()` (index lookup)
- 1 000 × `get_block_header()` (RocksDB read)
- 1 000 × `get_block_uncles()` (RocksDB read)
- 1 000 × `get_block_extension()` (RocksDB read)
- 1 × `mmr.gen_proof(1 000 positions)` (CPU-bound)

A single attacker connection sending these messages in a tight loop saturates server RocksDB I/O and CPU. No ban is ever issued and no connection-level throttle exists. This matches **High (10 001–15 000 points): Vulnerabilities or bad designs which could cause CKB network congestion / crash a CKB node with few costs**.

## Likelihood Explanation

The light client protocol is opt-in on production CKB full nodes. Any peer that can establish a TCP connection and speak the light client protocol can execute this attack. No authentication, no PoW, no stake, and no prior trust is required. The attack is trivially scriptable: collect 1 000 valid main-chain block hashes once, then replay the same `GetBlocksProof` message in a loop. The absence of any ban or rate-limit means the attacker can sustain the attack indefinitely from a single connection.

## Recommendation

Add a per-peer, per-message-type rate limiter to `LightClientProtocol`, mirroring the pattern in `Relayer`:

1. Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` using `governor::RateLimiter` with a `HashMapStateStore`.
2. In `try_process()`, check `rate_limiter.check_key(&(peer_index, message.item_id()))` before dispatching any handler.
3. Add `TooManyRequests = 429` to `StatusCode` and return it on limit breach (this is a 4xx code, which triggers the existing ban logic).
4. Additionally consider reducing `GET_BLOCKS_PROOF_LIMIT` or requiring paginated responses to lower the per-message amplification factor.

## Proof of Concept

1. Run a CKB full node with light client protocol enabled and a chain of height ≥ 1 000.
2. From a separate process, connect as a light client peer.
3. Collect 1 000 distinct valid main-chain block hashes (`h1…h1000`) and the current tip hash.
4. In a tight loop, send `GetBlocksProof { block_hashes: [h1…h1000], last_hash: tip }`.
5. Observe: server CPU and RocksDB I/O spike to saturation; no ban is issued against the sending peer; normal sync/relay throughput degrades.

The same 1 000-hash payload can be reused across iterations since the hashes remain valid main-chain hashes. The server will process each message fully, return `Status::ok()`, and never ban the sender.

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

**File:** util/light-client-protocol-server/src/lib.rs (L207-216)
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
```

**File:** util/light-client-protocol-server/src/status.rs (L16-38)
```rust
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

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
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
