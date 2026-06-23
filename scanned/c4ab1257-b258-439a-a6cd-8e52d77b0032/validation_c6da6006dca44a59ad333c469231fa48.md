The code path is fully traceable and the gap is real. Here is the assessment:

---

### Title
Rate-Limit Bypass via Non-Banning `TooManyRequests` Status Code Allows Persistent Relay Message Flooding — (`sync/src/relayer/mod.rs`, `sync/src/status.rs`)

### Summary

`StatusCode::TooManyRequests = 110` is a 1xx code. `Status::should_ban()` only returns a ban duration for codes in `400..500`. As a result, a peer that continuously exceeds the relay rate limit is never disconnected — it receives only a `debug!` log entry and can immediately retry, indefinitely.

### Finding Description

`Relayer::try_process()` checks the rate limiter keyed by `(PeerIndex, message_type)` at 30 req/s. When the check fails, it returns `StatusCode::TooManyRequests.with_context(message.item_name())`: [1](#0-0) 

`Relayer::process()` then calls `status.should_ban()` on that returned status: [2](#0-1) 

`should_ban()` gates entirely on the `400..500` range: [3](#0-2) 

Since `TooManyRequests = 110` is outside that range, `should_ban()` returns `None`. `nc.ban_peer()` is never called. The peer falls through to the `debug!` branch and is free to retry immediately. [4](#0-3) 

### Impact Explanation

The attacker occupies a peer slot indefinitely and can sustain an unbounded message flood. Every inbound message — even one rejected by the rate limiter — still requires: network receive, molecule deserialization up to `item_id()`, and a hashmap lookup in the rate limiter. At high send rates this imposes measurable CPU and I/O overhead on the victim node with zero cost to the attacker beyond bandwidth. The peer slot is also permanently consumed, crowding out legitimate peers.

### Likelihood Explanation

Any connected peer can trigger this. No special privilege, key, or hashpower is required. The attacker only needs a single TCP connection to the target node and the ability to send relay protocol messages faster than 30/s — trivially achievable with a standard CKB client or raw socket.

### Recommendation

Either:
1. Move `TooManyRequests` into the 4xx range (e.g., `TooManyRequests = 429`) so `should_ban()` covers it automatically, or
2. Add an explicit arm in `should_ban()` for `StatusCode::TooManyRequests` returning a ban duration, or
3. Handle the rate-limit rejection before returning a `Status` (as the `HolePunching` protocol does — it `return;`s immediately at the rate-limit check site and never propagates a `TooManyRequests` status to `should_ban()`). [5](#0-4) 

### Proof of Concept

1. Connect to a CKB node as a peer.
2. Send `GetRelayTransactions` (or any non-`CompactBlock` relay message) at >30 req/s for 60 seconds.
3. Observe: the node logs `debug … TooManyRequests` for each excess message but never calls `ban_peer`. The peer remains connected throughout.
4. Compare with a patched version where `TooManyRequests` triggers `should_ban()`: the peer is disconnected after the first rate-limit violation.

The invariant — that a peer persistently violating the rate limit must be disconnected — is broken because `StatusCode::TooManyRequests = 110` is numerically outside the `400..500` guard in `should_ban()`. [6](#0-5)

### Citations

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

**File:** sync/src/relayer/mod.rs (L195-204)
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

**File:** sync/src/status.rs (L41-68)
```rust
pub enum StatusCode {
    ///////////////////////////////////
    //      Informational 1xx        //
    ///////////////////////////////////
    /// OK
    OK = 100,
    /// Ignored
    Ignored = 101,
    /// The node had already received and recorded this block as pending block
    CompactBlockIsAlreadyPending = 102,
    /// The node is requesting from other peers for this block, but no response yet
    CompactBlockIsAlreadyInFlight = 103,
    /// The node had already stored this block into database
    CompactBlockAlreadyStored = 104,
    /// The CompactBlock is older than what the node expects
    CompactBlockIsStaled = 105,
    /// The node cannot process the arrived CompactBlock successfully for lack
    /// of information of its parent
    CompactBlockRequiresParent = 106,
    /// The node cannot process the arrived CompactBlock successfully for lack
    /// of parts of its transactions
    CompactBlockRequiresFreshTransactions = 107,
    /// CompactBlock short-ids collision
    CompactBlockMeetsShortIdsCollision = 108,
    /// In-flight blocks limit exceeded
    BlocksInFlightReachLimit = 109,
    /// Generic rate limit error
    TooManyRequests = 110,
```

**File:** sync/src/status.rs (L165-180)
```rust
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
