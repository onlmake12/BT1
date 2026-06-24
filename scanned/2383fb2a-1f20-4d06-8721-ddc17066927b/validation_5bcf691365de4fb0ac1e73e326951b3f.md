Audit Report

## Title
Missing Per-Peer Rate Limiter on BlockFilter Protocol Allows Single-Peer DoS via Unbounded DB Read Amplification — (`sync/src/filter/mod.rs`)

## Summary

The `BlockFilter` protocol handler processes inbound `GetBlockFilterHashes`, `GetBlockFilters`, and `GetBlockFilterCheckPoints` requests with no per-peer rate limiting at any layer. In contrast, the `Relayer` protocol enforces a hard cap of 30 req/s per `(PeerIndex, message_item_id)`. Because each `BlockFilter` request triggers up to 4000 synchronous RocksDB reads and the Filter protocol is enabled by default, a single unprivileged peer can saturate the node's I/O, starving sync and relay pipelines of DB access.

## Finding Description

**Asymmetric rate-limit enforcement between `Relayer` and `BlockFilter`:**

`Relayer` declares a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and initializes it at 30 req/s in `Relayer::new`: [1](#0-0) 

`Relayer::try_process` checks this limiter before dispatching every non-PoW message: [2](#0-1) 

`BlockFilter`'s struct contains only `shared: Arc<SyncShared>` — no rate limiter field exists: [3](#0-2) 

`BlockFilter::received` parses the message and immediately calls `self.process(...)` with no rate-limit check: [4](#0-3) 

`BlockFilter::try_process` dispatches directly to handlers with no guard: [5](#0-4) 

**Per-request DB amplification:**

`GetBlockFilterHashesProcess::execute` loops up to `BATCH_SIZE = 2000`, performing two DB reads per iteration (`get_block_hash` + `get_block_filter_hash`): [6](#0-5) [7](#0-6) 

`GetBlockFilterCheckPointsProcess::execute` has the same `BATCH_SIZE = 2000` loop with identical DB access pattern: [8](#0-7) [9](#0-8) 

`GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE = 1000` reading full filter data (up to 1.8 MB per response): [10](#0-9) [11](#0-10) 

**Filter is enabled by default:**

`default_support_all_protocols()` includes `SupportProtocol::Filter`: [12](#0-11) 

The launcher registers the `BlockFilter` handler unconditionally when `Filter` is present in `support_protocols`: [13](#0-12) 

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points).**

A single peer sending `GetBlockFilterHashes{start_number: 0}` in a tight loop triggers up to 4000 RocksDB reads per frame (2000 × `get_block_hash` + `get_block_filter_hash`). At 100 req/s this is 400,000 DB reads/s from one peer alone. This saturates the node's I/O subsystem, causing the sync and relay pipelines to stall on DB access, which delays block propagation and degrades the node's participation in the network. The attacker cost is negligible: a tiny (~12-byte) request frame per 4000 DB reads.

## Likelihood Explanation

The Filter protocol is on by default in both `default_support_all_protocols()` and `resource/ckb.toml`. The `BLOCK_FILTER` capability flag is advertised in the `Identify` handshake, making the target discoverable by any peer. No PoW, stake, or privileged role is required. Any peer that completes the tentacle handshake can immediately begin flooding. The attack is repeatable and stateless from the attacker's perspective.

## Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`), initialize it at 30 req/s in `BlockFilter::new`, and check it at the top of `BlockFilter::try_process` keyed by `(peer, message.item_id())`, returning `StatusCode::TooManyRequests` on failure — identical to the guard in `Relayer::try_process` at lines 116–123 of `sync/src/relayer/mod.rs`.

## Proof of Concept

```
1. Connect to a CKB full node running with default config (Filter protocol enabled).
2. Negotiate the /ckb/filter protocol (ID 121) via tentacle handshake.
3. In a tight loop, send GetBlockFilterHashes{start_number: 0} frames (~12 bytes each).
   - The node processes each with up to 4000 RocksDB reads (2000 × get_block_hash
     + get_block_filter_hash), with no rate-limit cutoff.
4. Observe: node I/O saturates; block sync and relay message latency increases.
5. Contrast: repeat with /ckb/relay3 (ID 101) sending GetBlockTransactions —
   after 30 req/s the node returns TooManyRequests and stops processing.
   No such cutoff exists for /ckb/filter.
```

### Citations

**File:** sync/src/relayer/mod.rs (L81-92)
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

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L33-68)
```rust
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

**File:** sync/src/filter/mod.rs (L151-152)
```rust
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L53-56)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L43-46)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-47)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
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

**File:** util/launcher/src/lib.rs (L443-456)
```rust
        if support_protocols.contains(&SupportProtocol::Filter) {
            let filter = BlockFilter::new(Arc::clone(&sync_shared));

            protocols.push(
                CKBProtocol::new_with_support_protocol(
                    SupportProtocols::Filter,
                    Box::new(filter),
                    Arc::clone(&network_state),
                )
                .compress(false),
            );
        } else {
            flags.remove(Flags::BLOCK_FILTER);
        }
```
