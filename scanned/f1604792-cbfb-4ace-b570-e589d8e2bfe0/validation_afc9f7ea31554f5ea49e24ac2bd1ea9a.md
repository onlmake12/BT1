### Title
Missing Peer Ban for Unsolicited Block Filter Response Messages Enables Unbounded Resource Exhaustion - (File: `sync/src/filter/mod.rs`)

### Summary

The `BlockFilter` protocol handler in `sync/src/filter/mod.rs` explicitly acknowledges (via a `// TODO: ban remote peer` comment) that it fails to penalize peers who send unsolicited response-type messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`). Unlike every other CKB P2P protocol handler, the Filter handler has no rate limiter and issues no ban for this class of protocol violation. An unprivileged peer can flood a full node with arbitrarily large, well-formed filter response messages indefinitely, consuming CPU, memory, and I/O without any consequence.

### Finding Description

The `BlockFilter` protocol handler's `try_process` function matches on all six `BlockFilterMessage` union variants. The three "request" variants (`GetBlockFilters`, `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`) are handled normally. The three "response" variants — which a server node should never receive from a peer because it never asked for them — fall into a catch-all arm:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    // remote peer should not send block filter to us without asking
    // TODO: ban remote peer
    warn_target!(...);
    Status::ignored()
}
``` [1](#0-0) 

`Status::ignored()` does not set a ban time. The outer `process()` function only calls `nc.ban_peer()` when `status.should_ban()` returns `Some(ban_time)`, which never happens for `Status::ignored()`. [2](#0-1) 

The `BlockFilter` struct carries no rate limiter field, unlike the `Relayer` (which has a `rate_limiter: RateLimiter<(PeerIndex, u32)>` capping 30 req/sec per peer per message type) and `HolePunching` (which has both a per-session rate limiter and a forward rate limiter). [3](#0-2) [4](#0-3) 

Every other protocol handler bans peers for protocol violations. The Sync handler bans for malformed `SendBlock` or excess fields. The Relay handler bans for malformed `CompactBlock`. The HolePunching handler bans for any parse failure. The Filter handler bans for parse failure at the outer `received()` level, but silently ignores the semantically invalid unsolicited response messages. [5](#0-4) 

### Impact Explanation

An attacker connects to a CKB full node and sends a continuous stream of well-formed (parseable) `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages over the Filter protocol. Each message:

1. Passes the outer `from_compatible_slice` parse check (no ban triggered there).
2. Enters `try_process`, is deserialized, triggers a `warn_target!` log write, and updates metrics via `metric_ckb_message_bytes`.
3. Returns `Status::ignored()` — no ban, no disconnect, no rate limit.

The attacker is never disconnected or banned. The `BlockFilters` table can carry up to ~1.8 MB of filter data per message (the server-side size cap in `get_block_filters_process.rs` applies only to outbound messages, not inbound). The attacker can send maximum-size messages at line rate, saturating the async handler task, exhausting log I/O, and starving legitimate light-client `GetBlockFilters` requests of processing time. [6](#0-5) 

### Likelihood Explanation

The Filter protocol (`SupportProtocols::Filter`) is registered and publicly reachable on any CKB full node that has block filter building enabled. No authentication, stake, or privilege is required to connect and send messages. The attack requires only a TCP connection and the ability to craft a valid molecule-encoded `BlockFilterMessage`. The `// TODO: ban remote peer` comment confirms the developers are aware of the gap but it has not been closed. [7](#0-6) 

### Recommendation

Replace `Status::ignored()` in the unsolicited-response arm with a status code that triggers a ban, consistent with how all other CKB protocol handlers treat protocol violations:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    StatusCode::UnexpectedMessage
        .with_context("peer sent unsolicited block filter response")
}
```

Additionally, add a per-peer rate limiter to `BlockFilter` (mirroring the `Relayer`'s `RateLimiter<(PeerIndex, u32)>`) so that even well-intentioned but misbehaving peers cannot saturate the handler before a ban is issued.

### Proof of Concept

1. Connect to a CKB full node on the Filter protocol sub-stream.
2. Construct a valid molecule-encoded `BlockFilterMessage` wrapping a `BlockFilters` table with `start_number = 0`, `block_hashes` and `filters` vectors filled to the ~1.8 MB limit with arbitrary bytes.
3. Send this message in a tight loop.
4. Observe: the node logs a warning for every message, updates metrics, and never disconnects or bans the sender. The Filter protocol handler's async task is continuously occupied processing attacker messages, delaying or starving legitimate `GetBlockFilters` responses to real light clients.

The attacker entry path is: unprivileged TCP peer → Tentacle P2P session → `BlockFilter::received()` → `from_compatible_slice` succeeds → `try_process` matches unsolicited-response arm → `Status::ignored()` → no ban → repeat indefinitely. [8](#0-7)

### Citations

**File:** sync/src/filter/mod.rs (L20-31)
```rust
/// Filter protocol handle
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}

impl BlockFilter {
    /// Create a new block filter protocol handler
    pub fn new(shared: Arc<SyncShared>) -> Self {
        Self { shared }
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

**File:** sync/src/filter/mod.rs (L122-160)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let msg = match packed::BlockFilterMessageReader::from_compatible_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                info_target!(
                    crate::LOG_TARGET_FILTER,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        debug_target!(
            crate::LOG_TARGET_FILTER,
            "received msg {} from {}",
            msg.item_name(),
            peer_index
        );
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
        debug_target!(
            crate::LOG_TARGET_FILTER,
            "process message={}, peer={}, cost={:?}",
            msg.item_name(),
            peer_index,
            Instant::now().saturating_duration_since(start_time),
        );
    }
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

**File:** sync/src/synchronizer/mod.rs (L940-948)
```rust
            _ => {
                info!("A malformed message from peer {}", peer_index);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
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
