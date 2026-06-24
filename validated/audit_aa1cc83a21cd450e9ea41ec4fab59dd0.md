All six cited claims have been verified against the actual source code:

1. `LightClientProtocol` struct contains only `shared: Shared` — confirmed at [1](#0-0) 
2. `try_process` dispatches directly with no rate-limiting logic — confirmed at [2](#0-1) 
3. `GetLastStateProcess::execute` calls `get_verifiable_tip_header()` unconditionally regardless of `subscribe` — confirmed at [3](#0-2) 
4. `get_verifiable_tip_header()` always performs a live `chain_root_mmr(...).get_root()` call — confirmed at [4](#0-3) 
5. `Relayer` has a `governor`-based `RateLimiter<(PeerIndex, u32)>` at 30 req/s — confirmed at [5](#0-4) 
6. `LightClientProtocol` has no equivalent guard — confirmed by absence.

---

Audit Report

## Title
Missing Per-Peer Rate Limit on `GetLastState` Allows Unbounded MMR RocksDB Reads — (`util/light-client-protocol-server/src/lib.rs`)

## Summary
`LightClientProtocol` has no rate-limiting guard in `try_process`, unlike `Relayer` which enforces 30 req/s per `(peer, message_type)`. Any peer can flood `GetLastState` messages at maximum socket speed, each triggering O(log N) live RocksDB MMR reads via `get_verifiable_tip_header()`, saturating the shared RocksDB read path and degrading block-processing throughput and P2P responsiveness on the targeted node.

## Finding Description
`LightClientProtocol` is a plain struct with only a `shared: Shared` field — no rate limiter is declared or initialized. `try_process` dispatches every incoming message directly to its handler with zero rate-limiting logic. For `GetLastState`, `GetLastStateProcess::execute` unconditionally calls `self.protocol.get_verifiable_tip_header()` regardless of the `subscribe` flag. That function acquires a snapshot, fetches the tip block, then calls `snapshot.chain_root_mmr(tip_block.number() - 1).get_root()`, which reads O(log N) MMR nodes from the shared RocksDB store. At a chain height of ~10M blocks, this is ~23 RocksDB reads per message. A single peer sending `GetLastState { subscribe: false }` in a tight loop at TCP line rate can sustain thousands of messages per second, producing tens of thousands of RocksDB reads per second from a single connection. The `Relayer` explicitly guards every non-PoW message with a `governor`-based `RateLimiter<(PeerIndex, u32)>` at 30 req/s; `LightClientProtocol` has no equivalent guard. The `max_inbound` cap (default 125) bounds the number of peers, not the per-peer message rate.

## Impact Explanation
This matches the High impact class: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** A single unprivileged peer can saturate the RocksDB read path shared with block verification, causing block-processing latency to spike and P2P responsiveness to degrade proportionally to message rate. Under sustained attack from even a handful of peers, the node can become effectively unresponsive to block propagation, which also matches **"Vulnerabilities which could easily crash a CKB node"** in the degenerate case of full I/O saturation.

## Likelihood Explanation
The attack requires only a standard P2P connection — no privileges, no PoW, no key material. `GetLastState` is a minimal fixed-size message (one boolean field), so a single TCP connection can sustain thousands of messages per second. The node must have `LightClient` in `support_protocols`, which is an opt-in but documented configuration. The attack is trivially repeatable and requires no special tooling beyond a basic P2P client.

## Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, initialize it in `new()` at 30 req/s per `(peer, message_type)` matching the `Relayer` pattern, and check it at the top of `try_process` before dispatching to any handler — returning `StatusCode::TooManyRequests` on violation, mirroring the guard in `Relayer::try_process`.

## Proof of Concept
1. Start a CKB full node with `LightClient` in `support_protocols`.
2. Connect a peer and send `GetLastState { subscribe: false }` in a tight loop at maximum socket speed.
3. Observe RocksDB read IOPS spike (via `rocksdb.stats`) and block-processing latency increase (via `ckb_chain_process_block_duration` metrics).
4. Compare against a peer sending `GetRelayTransactions` at the same rate — the `Relayer`'s 30 req/s cap will throttle it and return `TooManyRequests`; the light-client handler will not throttle and will continue processing every message.

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

**File:** util/light-client-protocol-server/src/lib.rs (L127-145)
```rust
    pub(crate) fn get_verifiable_tip_header(&self) -> Result<packed::VerifiableHeader, String> {
        let snapshot = self.shared.snapshot();

        let tip_hash = snapshot.tip_hash();
        let tip_block = snapshot
            .get_block(&tip_hash)
            .expect("checked: tip block should be existed");
        let parent_chain_root = if tip_block.is_genesis() {
            Default::default()
        } else {
            let mmr = snapshot.chain_root_mmr(tip_block.number() - 1);
            match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return Err(errmsg);
                }
            }
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-45)
```rust
    pub(crate) async fn execute(self) -> Status {
        let subscribe: bool = self.message.subscribe().into();
        if subscribe {
            self.nc.with_peer_mut(
                self.peer,
                Box::new(|peer| {
                    peer.if_lightclient_subscribed = true;
                }),
            );
        }

        let tip_header = match self.protocol.get_verifiable_tip_header() {
            Ok(tip_state) => tip_state,
            Err(errmsg) => {
                return StatusCode::InternalError.with_context(errmsg);
            }
        };
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
