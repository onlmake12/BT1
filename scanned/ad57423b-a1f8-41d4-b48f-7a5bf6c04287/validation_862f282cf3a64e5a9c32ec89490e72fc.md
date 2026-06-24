Audit Report

## Title
Unbounded Per-Message RocksDB + MMR Amplification DoS in Light Client `GetBlocksProof` Handler — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary

The `GetBlocksProof` handler in `LightClientProtocol` accepts messages containing up to 1 000 valid block hashes and performs 3 000+ synchronous RocksDB reads plus one MMR proof generation per message, with no per-peer rate limiting. Because well-formed requests always return `Status::ok()` (HTTP 200), no ban is ever issued, and an attacker can flood the server in a tight loop from a single unauthenticated connection, saturating I/O and CPU.

## Finding Description

`GET_BLOCKS_PROOF_LIMIT` is set to 1 000: [1](#0-0) 

`execute()` rejects messages with **more than** 1 000 hashes (triggering a ban), but a message with **exactly** 1 000 valid hashes passes all guards: [2](#0-1) 

For every hash in `found`, the handler performs three synchronous RocksDB reads — `get_block_header`, `get_block_uncles`, `get_block_extension` — plus the earlier `is_main_chain` partition scan: [3](#0-2) 

`reply_proof()` then calls `mmr.gen_proof(items_positions)` with up to 1 000 leaf positions: [4](#0-3) 

`LightClientProtocol` carries **no** `rate_limiter` field and `try_process()` performs **no** rate check before dispatching `GetBlocksProof`: [5](#0-4) [6](#0-5) 

A successful request returns `Status::ok()` (code 200). The ban logic only fires on 4xx codes, so a valid max-size request never triggers a ban: [7](#0-6) 

The `StatusCode` enum also has no `TooManyRequests` variant, so there is no existing mechanism to rate-limit and ban: [8](#0-7) 

By contrast, the `Relayer` protocol explicitly rate-limits every non-PoW message per `(peer, item_id)` before processing: [9](#0-8) 

The light client protocol server has no equivalent guard.

## Impact Explanation

Each max-size `GetBlocksProof` message forces the server to execute:
- 1 000 × `is_main_chain()` (index lookup)
- 1 000 × `get_block_header()` (RocksDB read)
- 1 000 × `get_block_uncles()` (RocksDB read)
- 1 000 × `get_block_extension()` (RocksDB read)
- 1 × `mmr.gen_proof(1 000 positions)` (CPU-bound MMR traversal)

A single attacker connection sending these messages in a tight loop saturates server I/O and CPU, degrading or halting normal sync and relay throughput. This maps to the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* and *"Vulnerabilities which could easily crash a CKB node"* (10 001–15 000 points).

## Likelihood Explanation

The light client protocol is opt-in on production CKB full nodes. Any peer that can establish a TCP connection and speak the protocol can execute this attack — no authentication, no PoW, no stake, and no prior trust is required. The attack is trivially scriptable: collect 1 000 valid main-chain block hashes once, then replay the same `GetBlocksProof` message in a loop. No ban is ever issued, so the attacker connection persists indefinitely.

## Recommendation

Add a per-peer, per-message-type rate limiter to `LightClientProtocol`, mirroring the pattern already used in `Relayer`: [10](#0-9) 

Specifically:
1. Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`.
2. Add a `TooManyRequests = 429` variant to `StatusCode` (4xx range so `should_ban()` fires).
3. In `try_process()`, check the limiter keyed on `(peer_index, message.item_id())` before dispatching any handler; return `StatusCode::TooManyRequests` on breach.
4. Additionally consider reducing `GET_BLOCKS_PROOF_LIMIT` or splitting large requests into paginated responses. [11](#0-10) 

## Proof of Concept

1. Run a CKB full node with light client protocol enabled and a chain of height ≥ 1 000.
2. From a separate process, connect as a light client peer.
3. Collect 1 000 valid main-chain block hashes and the current tip hash.
4. In a tight loop, send `GetBlocksProof { block_hashes: [h1…h1000], last_hash: tip }`.
5. Observe: server CPU and RocksDB I/O spike to saturation; no ban is issued against the sending peer; normal sync/relay throughput degrades. [12](#0-11)

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L33-114)
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

        let proved_items = (
            block_headers.into(),
            uncles_hash.into(),
            packed::BytesOptVec::new_builder().set(extensions).build(),
        );
        let missing_items = missing.into();

        self.protocol
            .reply_proof::<packed::SendBlocksProofV1>(
                self.peer,
                self.nc,
                &last_block,
                positions,
                proved_items,
                missing_items,
            )
            .await
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

**File:** util/light-client-protocol-server/src/status.rs (L95-101)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
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
