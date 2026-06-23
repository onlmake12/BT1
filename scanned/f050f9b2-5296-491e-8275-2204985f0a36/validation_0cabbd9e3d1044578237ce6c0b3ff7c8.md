Let me check the `StatusCode::Ignored` numeric value and whether the `BlockFilter` handler has any rate limiter.

### Title
Missing Peer Ban and Rate Limiter for Unsolicited BlockFilter Response Messages — (`sync/src/filter/mod.rs`)

### Summary

The `BlockFilter` protocol handler accepts unsolicited response-type messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) from any connected peer, logs a warning, and returns `Status::ignored()` — a 1xx informational code that triggers neither a ban nor a rate-limit check. Unlike the `Relayer` and `HolePunching` handlers, the `BlockFilter` handler has no per-peer rate limiter. A single unprivileged peer can therefore flood the node with large, well-formed messages indefinitely at zero cost to the attacker.

---

### Finding Description

In `sync/src/filter/mod.rs`, `try_process` matches unsolicited response messages and explicitly defers the ban with a TODO:

```rust
// remote peer should not send block filter to us without asking
// TODO: ban remote peer
warn_target!(...);
Status::ignored()
``` [1](#0-0) 

`Status::ignored()` resolves to `StatusCode::Ignored = 101`. The `should_ban()` guard only fires for codes 400–499, and `should_warn()` only fires for codes ≥ 500, so the `process()` dispatcher applies no ban and emits no second warning: [2](#0-1) 

The `BlockFilter` struct carries no `rate_limiter` field at all: [3](#0-2) 

This is in direct contrast to the `Relayer`, which has a `RateLimiter<(PeerIndex, u32)>` capped at 30 req/s per (peer, message-type): [4](#0-3) 

And the `HolePunching` handler, which also has a per-session rate limiter checked before any dispatch: [5](#0-4) 

The `BlockFilters` message schema allows a `filters` field (a `BytesVec`) whose encoded size is bounded only by the tentacle frame limit (~2 MB). The outgoing path was recently capped at 1.8 MB precisely because larger frames caused disconnects: [6](#0-5) 

An attacker can craft valid `BlockFilters` messages up to ~1.8 MB each and send them in a tight loop. Every message is fully parsed, triggers one `warn_target!` log write, and is then discarded — with no ban and no rate cap.

---

### Impact Explanation

- **Network ingress saturation**: A single peer can push ~1.8 MB × N messages/s into the node's receive buffer. Because the tentacle transport does not apply per-protocol rate limits, the only natural brake is TCP flow control, which the attacker controls by adjusting send rate.
- **Log I/O amplification**: Each message unconditionally calls `warn_target!` inside `try_process`, writing a log line per message. At high message rates this can fill disk or degrade I/O performance.
- **Zero attacker cost**: No PoW, no stake, no valid chain state is required. The attacker only needs a valid P2P handshake on the Filter protocol.
- **No self-healing**: Because no ban is ever issued, the peer is never disconnected by the protocol layer. The attack persists for the lifetime of the TCP session.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to a node that has the Filter protocol enabled. The `BlockFilterMessage` molecule schema is public. Crafting a maximal-size `BlockFilters` message is trivial. The TODO comment confirms the developers are aware the ban is absent. Any node reachable on its P2P port is exposed.

---

### Recommendation

1. **Immediate**: Return a ban-triggering status code (4xx) instead of `Status::ignored()` for unsolicited response messages, removing the TODO:
   ```rust
   StatusCode::ProtocolMessageIsMalformed
       .with_context("unexpected BlockFilters/BlockFilterHashes/BlockFilterCheckPoints")
   ```
2. **Defense-in-depth**: Add a `RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct, mirroring the `Relayer` pattern, so that even legitimate peers cannot exceed a reasonable message rate.

---

### Proof of Concept

1. Connect to a CKB full node with the Filter protocol (`SupportProtocols::Filter`).
2. In a tight loop, send well-formed `BlockFilters` messages with a `filters` field padded to ~1.8 MB.
3. Observe: the peer is never banned; `warn_target!` fires for every message; network ingress and log volume grow without bound.
4. Confirm: the `Relayer` protocol under the same test is rate-limited to 30 req/s per peer; the `BlockFilter` protocol has no such cap. [1](#0-0) [7](#0-6)

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
