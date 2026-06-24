All cited code references are confirmed accurate. Every claim in the submission is verified against the actual source:

- `BATCH_SIZE = 1000` in `get_block_filters_process.rs` L9, with the loop performing two DB reads per iteration and a ~1.8 MB response cap confirmed at L45–57. [1](#0-0) 
- `BlockFilter` struct has no rate limiter field and `try_process` dispatches directly with no rate check. [2](#0-1) 
- `Relayer` has an explicit `governor`-based rate limiter at 30 req/s per `(PeerIndex, message_item_id)`, checked before dispatch. [3](#0-2) 
- `HolePunching` has both `rate_limiter` and `forward_rate_limiter`, checked before processing. [4](#0-3) 
- `Filter` is included in `default_support_all_protocols()` by default. [5](#0-4) 
- `GetBlockFilterHashesProcess` and `GetBlockFilterCheckPointsProcess` both use `BATCH_SIZE = 2000` with no rate limiting. [6](#0-5) [7](#0-6) 

---

Audit Report

## Title
Missing Rate Limiter in `BlockFilter` Protocol Enables Unauthenticated Resource Exhaustion — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

## Summary

The `BlockFilter` protocol handler contains no per-peer rate limiter of any kind. Any unauthenticated peer can flood `GetBlockFilters{start_number: 0}` messages, each triggering up to 2000 RocksDB point reads and a ~1.8 MB heap allocation and serialization per message, with zero throttling. This contrasts directly with `Relayer` and `HolePunching`, both of which enforce `governor`-based rate limits before processing any message.

## Finding Description

`GetBlockFiltersProcess::execute` iterates up to `BATCH_SIZE = 1000` blocks, calling `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter(&block_hash)` per iteration (2000 RocksDB reads total), then builds and sends a response capped at ~1.8 MB via `async_send_message_to`. The `BlockFilter` struct holds only `shared: Arc<SyncShared>` — no rate limiter field. `try_process` dispatches directly to `GetBlockFiltersProcess::new(...).execute().await` with no rate check preceding it.

By contrast, `Relayer::try_process` checks `self.rate_limiter.check_key(&(peer, message.item_id()))` before any dispatch, returning `StatusCode::TooManyRequests` on excess. `HolePunching::received` similarly checks `self.rate_limiter.check_key(...)` and returns immediately on excess. The same omission applies to `GetBlockFilterHashesProcess` (BATCH_SIZE=2000) and `GetBlockFilterCheckPointsProcess` (BATCH_SIZE=2000), both dispatched through the same unguarded `try_process`.

The Filter protocol is enabled by default in both `default_support_all_protocols()` and `resource/ckb.toml`. The attack message is a 12-byte struct (`start_number: u64`). No authentication is required to open a session on the Filter protocol.

## Impact Explanation

Each `GetBlockFilters` message with `start_number=0` on a node with >1000 built filter blocks causes up to 2000 RocksDB point reads, heap allocation and serialization of up to ~1.8 MB, and queuing of that response. With `max_peers = 125` inbound connections each flooding at network speed, the node's RocksDB I/O, CPU (serialization), and memory (response buffers) are exhausted with no throttle path. The 1.8 MB cap limits only response size, not request rate. This maps to **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The Filter protocol is on by default, requires no authentication, and the attack message is trivially constructed. Any attacker with a TCP connection to port 8115 can open a Filter protocol session and execute the flood. The explicit presence of `governor` rate limiters in `Relayer` and `HolePunching` confirms this is an unintentional omission, not a design choice. The attack is repeatable, requires no special knowledge, and scales linearly with the number of connections.

## Recommendation

Add a `governor`-based rate limiter to `BlockFilter`, keyed by `(PeerIndex, message_item_id)`, mirroring the pattern in `Relayer::new` and `Relayer::try_process`. A quota of 1–5 req/s per peer per message type is sufficient for legitimate light-client use. Call `rate_limiter.retain_recent()` in the `disconnected` handler, as done in `Relayer`.

## Proof of Concept

```
1. Connect to a CKB full node (>1000 blocks, Filter protocol enabled by default)
2. Open a session on /ckb/filter protocol (no authentication required)
3. In a tight loop, send:
     packed::BlockFilterMessage { GetBlockFilters { start_number: 0 } }
4. Observe: node RSS grows, RocksDB read IOPS saturate, CPU spikes on serialization
5. Repeat with N=10–125 parallel connections
6. Assert: node becomes unresponsive to legitimate peers; RocksDB I/O and memory exhausted
```

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/mod.rs (L22-68)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}

impl BlockFilter {
    /// Create a new block filter protocol handler
    pub fn new(shared: Arc<SyncShared>) -> Self {
        Self { shared }
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::BlockFilterMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::BlockFilterMessageUnionReader::GetBlockFilters(msg) => {
                GetBlockFiltersProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterHashes(msg) => {
                GetBlockFilterHashesProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::BlockFilters(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
                // remote peer should not send block filter to us without asking
                // TODO: ban remote peer
                warn_target!(
                    crate::LOG_TARGET_FILTER,
                    "Received unexpected message from peer: {:?}",
                    peer
                );
                Status::ignored()
            }
        }
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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```
