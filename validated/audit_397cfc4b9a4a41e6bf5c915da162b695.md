Audit Report

## Title
Missing Peer Ban and Rate Limiter for Unsolicited BlockFilter Response Messages — (`sync/src/filter/mod.rs`)

## Summary
The `BlockFilter` protocol handler accepts unsolicited response-type messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) from any connected peer without banning or rate-limiting the sender. The handler returns `Status::ignored()` (`StatusCode::Ignored = 101`), which falls outside the 4xx ban range and the 5xx warn range, so the `process()` dispatcher takes no enforcement action. A single peer can flood the node with large, well-formed messages indefinitely at zero cost, causing network ingress saturation, log disk exhaustion, and CPU load from repeated message parsing.

## Finding Description
In `sync/src/filter/mod.rs`, `try_process` matches the three unsolicited response variants and explicitly defers the ban with a TODO:

```rust
// remote peer should not send block filter to us without asking
// TODO: ban remote peer
warn_target!(...);
Status::ignored()
``` [1](#0-0) 

`Status::ignored()` resolves to `StatusCode::Ignored = 101`. [2](#0-1) 

The `process()` dispatcher calls `should_ban()`, which only returns `Some` for codes in `400..500`, and `should_warn()`, which only fires for codes `>= 500`. Code 101 satisfies neither, so no ban is issued and no second warning is emitted: [3](#0-2) 

The `BlockFilter` struct carries no `rate_limiter` field: [4](#0-3) 

By contrast, `Relayer` has a `RateLimiter<(PeerIndex, u32)>` capped at 30 req/s per (peer, message-type), checked before any dispatch: [5](#0-4) 

`HolePunching` similarly checks its rate limiter before dispatch: [6](#0-5) 

The outgoing `BlockFilters` response is capped at ~1.8 MB to avoid tentacle frame-size disconnects, confirming that individual messages can approach this size: [7](#0-6) 

An attacker crafts valid `BlockFilters` messages padded to ~1.8 MB and sends them in a tight loop. Each message is fully deserialized by `packed::BlockFilterMessageReader::from_compatible_slice`, triggers one `warn_target!` log write inside `try_process`, and is discarded — with no ban and no rate cap. The peer is never disconnected by the protocol layer. [8](#0-7) 

## Impact Explanation
This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, and potentially **High: Vulnerabilities which could easily crash a CKB node**.

- **Network ingress saturation**: A single peer can push ~1.8 MB × N msg/s into the node's receive buffer. The only natural brake is TCP flow control, which the attacker controls by adjusting send rate.
- **Log disk exhaustion**: `warn_target!` fires unconditionally for every message inside `try_process`. At high message rates this fills disk, which can crash the node or corrupt the database.
- **CPU load**: Full molecule deserialization of a ~1.8 MB frame occurs for every message before the unsolicited-response branch is reached.
- **No self-healing**: Because no ban is ever issued, the peer is never disconnected by the protocol layer. The attack persists for the lifetime of the TCP session.

## Likelihood Explanation
The attack requires only a standard P2P connection to a node with the Filter protocol enabled. The `BlockFilterMessage` molecule schema is public. Crafting a maximal-size `BlockFilters` message is trivial. The TODO comment in the source confirms the developers are aware the ban is absent. No PoW, no stake, and no valid chain state is required — only a valid P2P handshake on the Filter protocol. [9](#0-8) 

## Recommendation
1. **Immediate**: Replace `Status::ignored()` with a ban-triggering 4xx status for unsolicited response messages, removing the TODO:
   ```rust
   StatusCode::ProtocolMessageIsMalformed
       .with_context("unexpected BlockFilters/BlockFilterHashes/BlockFilterCheckPoints")
   ```
2. **Defense-in-depth**: Add a `RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct, mirroring the `Relayer` pattern, and check it at the top of `try_process` before any dispatch — including for the legitimate request-type messages (`GetBlockFilters`, `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`).

## Proof of Concept
1. Connect to a CKB full node with `SupportProtocols::Filter` enabled.
2. In a tight loop, send well-formed `BlockFilters` messages with a `filters` field padded to ~1.8 MB (matching the outgoing cap in `get_block_filters_process.rs`).
3. Observe: the peer is never banned; `warn_target!` fires for every message; network ingress, CPU, and log volume grow without bound.
4. Confirm: under the same test, the `Relayer` protocol is rate-limited to 30 req/s per peer; the `BlockFilter` protocol has no such cap.
5. Disk exhaustion can be confirmed by monitoring the log file size; node crash occurs when the filesystem fills or the I/O scheduler is saturated.

### Citations

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
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

**File:** sync/src/filter/mod.rs (L128-143)
```rust
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
```

**File:** sync/src/status.rs (L46-48)
```rust
    OK = 100,
    /// Ignored
    Ignored = 101,
```

**File:** sync/src/status.rs (L164-185)
```rust
    /// Whether should ban session
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
    }

    /// Whether should output a warning log
    pub fn should_warn(&self) -> bool {
        self.code as u16 >= 500
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

**File:** sync/src/filter/get_block_filters_process.rs (L48-57)
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
                        }
```
