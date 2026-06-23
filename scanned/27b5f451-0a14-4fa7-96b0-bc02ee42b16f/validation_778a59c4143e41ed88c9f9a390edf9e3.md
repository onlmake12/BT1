The code is clear and the analysis is straightforward. Let me confirm the exact logic.

**`should_ban()` in `sync/src/status.rs` line 166:**
```rust
if !(400..500).contains(&(self.code as u16)) {
    return None;
}
```
`TooManyRequests = 110` → not in `[400, 500)` → returns `None`.

**`should_warn()` in `sync/src/status.rs` line 184:**
```rust
self.code as u16 >= 500
```
`110 >= 500` is `false`.

**`process()` in `sync/src/relayer/mod.rs` lines 195–221:**
```rust
if let Some(ban_time) = status.should_ban() { nc.ban_peer(...) }
else if status.should_warn() { warn!(...) }
else if !status.is_ok() { debug!(...) }  // ← TooManyRequests lands here
```

The path is confirmed: repeated rate-limit violations produce only a debug log, no ban, no disconnect.

---

### Title
Missing ban enforcement for rate-limit violations allows indefinite message flooding — (`sync/src/relayer/mod.rs`, `sync/src/status.rs`)

### Summary
`StatusCode::TooManyRequests` (110) sits in the 1xx informational range. `should_ban()` only acts on 4xx codes and `should_warn()` only on 5xx codes, so every rate-limit violation silently falls through to a debug log. A connected peer can send relay messages above the 30 req/sec cap indefinitely with zero enforcement consequence.

### Finding Description
`Relayer::new` configures a per-`(PeerIndex, message_type)` token-bucket rate limiter capped at 30 req/sec. [1](#0-0) 

When the bucket is exhausted, `try_process` returns `StatusCode::TooManyRequests.with_context(item_name)` immediately. [2](#0-1) 

`process()` then calls `should_ban()` and `should_warn()` on that status. `should_ban()` gates on the 4xx range: [3](#0-2) 

`should_warn()` gates on ≥ 500: [4](#0-3) 

`TooManyRequests = 110` satisfies neither condition, so `process()` emits only a debug log and returns — no `nc.ban_peer()` call is ever made. [5](#0-4) 

### Impact Explanation
The rate limiter correctly drops processing of excess messages, but the node must still **receive, deserialize, and rate-check** every inbound message. A single connected peer can sustain an arbitrarily high send rate (e.g., thousands of `RelayTransactions` or `GetRelayTransactions` messages per second). The node pays the cost of network I/O, message deserialization (`from_compatible_slice`), and hash-map lookup on every packet, with no mechanism to shed that load by disconnecting the offending peer. This enables low-cost, sustained network-layer congestion against a targeted node.

The "probe oracle" aspect is secondary but real: because the only observable difference between an accepted and a rate-limited message is the presence or absence of a downstream response, the attacker can binary-search the token bucket state and maintain a send rate of exactly 30 req/sec per message type across all message types simultaneously, maximizing throughput while staying invisible to any warn/ban logic.

### Likelihood Explanation
Any connected peer can trigger this. No special privilege, key, or hashpower is required. The attacker needs only a standard P2P connection and the ability to send relay protocol messages at high rate, which is trivially achievable.

### Recommendation
Move `TooManyRequests` into the 4xx or 5xx range, **or** add a separate repeated-violation counter per peer that calls `nc.ban_peer()` after N consecutive rate-limit hits within a sliding window. The simplest fix is to change `should_ban()` to also return a ban duration for `StatusCode::TooManyRequests`:

```rust
match self.code {
    StatusCode::TooManyRequests => Some(SYNC_USELESS_BAN_TIME),
    StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
    _ => Some(BAD_MESSAGE_BAN_TIME),
}
```
and remove the early-return guard so 1xx codes reach the match arm, or simply reclassify `TooManyRequests` as a 4xx code.

### Proof of Concept
1. Connect a peer to a CKB node.
2. Send `GetRelayTransactions` messages at 31/sec for 60 seconds (one message type, one peer).
3. Observe: the node emits only debug-level logs; the peer is never banned or disconnected.
4. Confirm: `nc.ban_peer` is never called for this peer during the entire window.
5. Expected (fixed) behavior: after the first rate-limit violation (or after N violations), the peer is banned for at least `SYNC_USELESS_BAN_TIME`.

### Citations

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
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

**File:** sync/src/relayer/mod.rs (L195-221)
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
        } else if status.should_warn() {
            warn_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, {}",
                item_name,
                peer,
                status
            );
        } else if !status.is_ok() {
            debug_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, {}",
                item_name,
                peer,
                status
            );
        }
```

**File:** sync/src/status.rs (L165-168)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
```

**File:** sync/src/status.rs (L183-185)
```rust
    pub fn should_warn(&self) -> bool {
        self.code as u16 >= 500
    }
```
