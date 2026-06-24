All code references check out exactly against the actual source. Let me confirm each claim:

- `TooManyRequests = 110` — confirmed at [1](#0-0) 
- `should_ban()` early-returns `None` for any code outside `[400, 500)` — confirmed at [2](#0-1) 
- `should_warn()` returns false for `110 >= 500` — confirmed at [3](#0-2) 
- Rate limiter set to 30 req/sec per `(PeerIndex, message_type)` — confirmed at [4](#0-3) 
- Rate-limit hit returns `TooManyRequests` without banning — confirmed at [5](#0-4) 
- `process()` only emits a debug log for `TooManyRequests` — confirmed at [6](#0-5) 

---

Audit Report

## Title
Missing ban enforcement for rate-limit violations allows indefinite relay message flooding — (`sync/src/relayer/mod.rs`, `sync/src/status.rs`)

## Summary
`StatusCode::TooManyRequests` (110) is classified as a 1xx informational code. `should_ban()` only acts on 4xx codes and `should_warn()` only on 5xx codes, so every rate-limit violation falls through to a debug log with no peer ban or disconnect. A connected peer can send relay messages above the 30 req/sec cap indefinitely, forcing the node to receive, deserialize, and rate-check every packet with zero enforcement consequence.

## Finding Description
`Relayer::new` configures a per-`(PeerIndex, message_type)` token-bucket rate limiter at 30 req/sec (`sync/src/relayer/mod.rs` L89–92). When the bucket is exhausted, `try_process` returns `StatusCode::TooManyRequests.with_context(item_name)` immediately (`sync/src/relayer/mod.rs` L116–123).

`process()` then evaluates the returned status:
- `should_ban()` (`sync/src/status.rs` L165–168) early-returns `None` for any code outside `[400, 500)`. Since `TooManyRequests = 110`, it always returns `None`.
- `should_warn()` (`sync/src/status.rs` L183–185) returns `false` because `110 >= 500` is false.
- The `else if !status.is_ok()` branch (`sync/src/relayer/mod.rs` L213–221) fires, emitting only a debug log.

`nc.ban_peer()` is never called. The peer remains connected and can repeat the flood indefinitely. The node must still receive, deserialize (`from_compatible_slice`), and perform a hashmap lookup for every inbound packet regardless of rate-limit outcome.

## Impact Explanation
Matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A single attacker with a standard P2P connection can sustain thousands of relay messages per second (e.g., `RelayTransactions`, `GetRelayTransactions`) against a targeted node. The node bears the full cost of network I/O, deserialization, and hashmap lookup on every packet with no mechanism to shed load by disconnecting the offending peer. Scaled across multiple target nodes simultaneously, this constitutes low-cost, sustained network-layer congestion.

## Likelihood Explanation
Any connected peer can trigger this. No special privilege, key, or hashpower is required — only a standard P2P connection and the ability to send relay protocol messages at high rate. The condition is trivially and repeatably achievable by any network participant.

## Recommendation
Reclassify `TooManyRequests` into the 4xx range, or extend `should_ban()` to explicitly handle it:

```rust
pub fn should_ban(&self) -> Option<Duration> {
    match self.code {
        StatusCode::TooManyRequests => Some(SYNC_USELESS_BAN_TIME),
        _ => {
            if !(400..500).contains(&(self.code as u16)) {
                return None;
            }
            match self.code {
                StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
                _ => Some(BAD_MESSAGE_BAN_TIME),
            }
        }
    }
}
```

Alternatively, reclassify `TooManyRequests` as a 4xx code (e.g., `419`) so it falls into the existing ban range without any logic change.

## Proof of Concept
1. Connect a peer to a CKB node via the standard P2P relay protocol.
2. Send `GetRelayTransactions` messages at 31/sec sustained for 60 seconds (one message type, one peer).
3. Observe: the node emits only `debug`-level log lines; the peer is never banned or disconnected.
4. Confirm: `nc.ban_peer` is never called for this peer during the entire window.
5. Expected (fixed) behavior: after the first rate-limit violation (or after N violations within a configurable window), the peer is banned for at least `SYNC_USELESS_BAN_TIME`.

### Citations

**File:** sync/src/status.rs (L67-68)
```rust
    /// Generic rate limit error
    TooManyRequests = 110,
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
