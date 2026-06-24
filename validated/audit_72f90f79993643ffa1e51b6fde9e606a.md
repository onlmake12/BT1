Audit Report

## Title
Missing Peer Ban and Rate Limiting for Unsolicited Filter Response Messages — (`sync/src/filter/mod.rs`)

## Summary
The `BlockFilter` protocol handler accepts unsolicited `BlockFilters`, `BlockFilterHashes`, and `BlockFilterCheckPoints` response messages from any connected peer, returns `Status::ignored()` (code 101), and never bans or rate-limits the sender. Because no application-layer defense exists, a single unprivileged peer can flood a node with large, well-formed filter response messages indefinitely, consuming inbound bandwidth and async handler CPU with no backpressure or disconnection.

## Finding Description
In `sync/src/filter/mod.rs`, `try_process` handles the three response-direction variants in a single arm that logs a warning and returns `Status::ignored()`: [1](#0-0) 

`Status::ignored()` constructs a `Status` with `StatusCode::Ignored = 101`: [2](#0-1) 

The `should_ban()` predicate only fires for codes in the range `400..500`: [3](#0-2) 

Because `101` is outside that range, `process()` never calls `nc.ban_peer()`: [4](#0-3) 

The `BlockFilter` struct carries no rate limiter field at all: [5](#0-4) 

This contrasts with `Relayer`, which has a `governor`-based rate limiter keyed by `(PeerIndex, message_type)` capped at 30 req/s: [6](#0-5) 

applied before any dispatch: [7](#0-6) 

The 1.8 MB size cap exists only on the *sender* side when constructing a `BlockFilters` response: [8](#0-7) 

An attacker is not bound by this cap when crafting their own messages. The TODO comment in the source explicitly acknowledges the missing ban: [9](#0-8) 

## Impact Explanation
An attacker who establishes connections to multiple CKB nodes and sends a continuous stream of well-formed `BlockFilterMessage{BlockFilters{...}}` frames can saturate inbound bandwidth and force repeated molecule deserialization and async dispatch on the Tokio runtime across all targeted nodes simultaneously. With no per-peer rate limit and no ban triggered, the attacker is never disconnected. Targeting a sufficient number of nodes degrades the P2P network's ability to propagate legitimate blocks and transactions, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. At the single-node level, sustained resource exhaustion can also degrade or crash a node, matching **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a standard TCP connection to the Filter protocol port and the ability to send valid molecule-encoded `BlockFilterMessage` frames. No proof-of-work, no key material, and no privileged role is needed. The attacker can reuse the same connection indefinitely since the peer is never banned. The TODO comment confirms the developers identified this gap but left it unresolved.

## Recommendation
1. Replace `Status::ignored()` with a ban-triggering `4xx` status code (e.g., `StatusCode::ProtocolMessageIsMalformed`) for unsolicited response messages, so the existing `process()` logic automatically calls `nc.ban_peer()`.
2. Add a per-peer rate limiter to `BlockFilter` mirroring the `Relayer`'s `governor::RateLimiter<(PeerIndex, u32)>` at 30 req/s, checked before the `match` dispatch in `try_process`.

## Proof of Concept
1. Connect to a CKB node with `SupportProtocols::Filter`.
2. In a tight loop, send molecule-encoded `BlockFilterMessage{BlockFilters{start_number: 0, block_hashes: [], filters: []}}` frames (valid encoding, minimal payload).
3. Observe: the peer is never disconnected or banned; the node logs only a `WARN` per message.
4. Scale to multiple target nodes simultaneously; observe inbound bandwidth and Tokio async task CPU rise proportionally to send rate with no application-layer throttle applied.

### Citations

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L55-66)
```rust
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
```

**File:** sync/src/filter/mod.rs (L88-97)
```rust
        if let Some(ban_time) = status.should_ban() {
            error_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, ban {:?} for {}",
                item_name,
                peer,
                ban_time,
                status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
```

**File:** sync/src/status.rs (L154-157)
```rust
    /// Ignored status
    pub fn ignored() -> Self {
        Self::new::<&str>(StatusCode::Ignored, None)
    }
```

**File:** sync/src/status.rs (L165-167)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
```

**File:** sync/src/relayer/mod.rs (L78-99)
```rust
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
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
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

**File:** sync/src/filter/get_block_filters_process.rs (L48-56)
```rust
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
```
