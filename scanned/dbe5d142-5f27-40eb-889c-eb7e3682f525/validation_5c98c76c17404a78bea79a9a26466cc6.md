### Title
Intended Peer Ban for Unsolicited Block Filter Response Messages Is Never Executed — (`sync/src/filter/mod.rs`)

### Summary

`BlockFilter::try_process()` in `sync/src/filter/mod.rs` explicitly documents the intent to ban a remote peer that sends unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages (`// TODO: ban remote peer`), but returns `Status::ignored()` instead of a ban-triggering status. The `process()` dispatcher only calls `nc.ban_peer()` when `status.should_ban()` returns `Some`, which only occurs for 4xx status codes. `StatusCode::Ignored = 101` is a 1xx code, so `should_ban()` always returns `None` for this case, and the peer is never banned. The ban mechanism is fully implemented and working for other cases in the same file, but the TODO was never completed for this specific misbehavior.

### Finding Description

In `sync/src/filter/mod.rs`, `BlockFilter::try_process()` handles incoming block filter protocol messages:

```
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    // remote peer should not send block filter to us without asking
    // TODO: ban remote peer
    warn_target!(...);
    Status::ignored()   // ← StatusCode::Ignored = 101
}
``` [1](#0-0) 

The `process()` dispatcher then evaluates the returned status:

```rust
if let Some(ban_time) = status.should_ban() {
    nc.ban_peer(peer, ban_time, status.to_string());
}
``` [2](#0-1) 

`should_ban()` in `sync/src/status.rs` only returns `Some` for status codes in the 400–499 range:

```rust
pub fn should_ban(&self) -> Option<Duration> {
    if !(400..500).contains(&(self.code as u16)) {
        return None;
    }
    ...
}
``` [3](#0-2) 

`StatusCode::Ignored = 101` is a 1xx code, so `should_ban()` always returns `None` for this arm, and `nc.ban_peer()` is never reached. The ban infrastructure is fully wired and functional — for example, malformed messages in the same `received()` handler correctly trigger an immediate ban:

```rust
nc.ban_peer(peer_index, BAD_MESSAGE_BAN_TIME, String::from("send us a malformed message"));
``` [4](#0-3) 

The `BAD_MESSAGE_BAN_TIME` constant is imported and available in the same file: [5](#0-4) 

The only missing piece is that the `// TODO: ban remote peer` was never implemented — the arm returns `Status::ignored()` instead of a 4xx status such as `StatusCode::ProtocolMessageIsMalformed.with_context(...)`. [6](#0-5) 

### Impact Explanation

An unprivileged remote peer connected on the block filter protocol (`SupportProtocols::Filter`) can send an unbounded stream of unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages. Each message is parsed, matched, and logged without any consequence to the sender. Because the peer is never banned or disconnected for this behavior, the attacker can sustain the flood indefinitely, consuming CPU (message parsing, logging, metrics recording) and network bandwidth on the victim node. The node has no automatic defense against this specific message class from a single peer. This is a resource-exhaustion / availability impact reachable by any external peer with no privileges.

### Likelihood Explanation

Any peer that can establish a connection on the filter protocol can trigger this. No special privileges, keys, or majority hashpower are required. The attacker simply connects and repeatedly sends valid-but-unsolicited filter response messages. The attack is trivially scriptable and can be sustained indefinitely since the peer is never banned.

### Recommendation

Replace `Status::ignored()` in the unsolicited-filter-response arm with a ban-triggering status code, completing the documented TODO:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    // remote peer should not send block filter to us without asking
    StatusCode::ProtocolMessageIsMalformed
        .with_context("received unsolicited block filter response")
}
```

This causes `should_ban()` to return `Some(BAD_MESSAGE_BAN_TIME)`, and the existing `process()` dispatcher will call `nc.ban_peer()` automatically, consistent with how every other protocol handler in the codebase handles misbehaving peers. [6](#0-5) 

### Proof of Concept

1. Attacker establishes a P2P connection to a CKB full node that has the block filter protocol enabled.
2. Attacker sends a stream of `BlockFilters` (or `BlockFilterHashes` / `BlockFilterCheckPoints`) messages without any prior `GetBlockFilters` request from the node.
3. Each message enters `BlockFilter::received()` → `process()` → `try_process()`, matches the unsolicited arm, logs a warning, and returns `Status::ignored()`.
4. `process()` calls `status.should_ban()` → returns `None` (code 101 is not in 400–499) → `nc.ban_peer()` is never called.
5. The attacker repeats indefinitely. The node logs warnings and records metrics for every message but never disconnects or bans the sender. [1](#0-0) [7](#0-6) [3](#0-2)

### Citations

**File:** sync/src/filter/mod.rs (L11-11)
```rust
use ckb_constant::sync::BAD_MESSAGE_BAN_TIME;
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

**File:** sync/src/filter/mod.rs (L136-141)
```rust
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
```

**File:** sync/src/status.rs (L73-74)
```rust
    /// Malformed protocol message
    ProtocolMessageIsMalformed = 400,
```

**File:** sync/src/status.rs (L154-157)
```rust
    /// Ignored status
    pub fn ignored() -> Self {
        Self::new::<&str>(StatusCode::Ignored, None)
    }
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
