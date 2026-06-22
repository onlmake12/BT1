### Title
Missing Peer Ban for Unsolicited Block Filter Messages Enables Unbounded P2P Resource Exhaustion - (File: `sync/src/filter/mod.rs`)

### Summary

The `BlockFilter` protocol handler in `sync/src/filter/mod.rs` explicitly acknowledges via a `// TODO: ban remote peer` comment that peers sending unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages should be banned, but the ban is never applied. Instead, `Status::ignored()` is returned, which carries status code `101` (Informational), causing the downstream `process()` dispatcher to take no enforcement action. Any unprivileged peer can flood a CKB full node with these messages indefinitely without being disconnected or banned.

### Finding Description

In `sync/src/filter/mod.rs`, the `try_process` function handles incoming `BlockFilterMessage` variants. For the three response-type messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) that a full node should never receive unsolicited, the code reads:

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

The returned `Status::ignored()` carries `StatusCode::Ignored = 101`. The `should_ban()` method on `Status` only triggers for codes in the `400..500` range:

```rust
pub fn should_ban(&self) -> Option<Duration> {
    if !(400..500).contains(&(self.code as u16)) {
        return None;
    }
    ...
}
``` [2](#0-1) 

So in the `process()` dispatcher, `status.should_ban()` returns `None`, and `nc.ban_peer(...)` is never called:

```rust
if let Some(ban_time) = status.should_ban() {
    nc.ban_peer(peer, ban_time, status.to_string());
} else if status.should_warn() { ... } else if !status.is_ok() { ... }
``` [3](#0-2) 

By contrast, every other protocol handler in the codebase (Sync, Relay, LightClient, HolePunching) bans peers for protocol violations. The `// TODO: ban remote peer` comment is the direct analog of the Lien Protocol's commented-out `require()` spam-protection checks — a security enforcement action that was deferred and never implemented.

### Impact Explanation

An unprivileged peer connecting over the Filter protocol can send an unlimited stream of `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages. For each message the node will:
1. Parse the molecule-encoded message (`from_compatible_slice`)
2. Dispatch through `try_process` and `process`
3. Emit a `warn_target!` log entry (I/O)
4. Record metrics via `metric_ckb_message_bytes`

Because no ban is ever applied, the attacker is never disconnected and can sustain this indefinitely. This constitutes a P2P resource exhaustion (CPU parsing, logging I/O, metrics overhead) reachable by any peer that can open a Filter protocol connection to the node.

### Likelihood Explanation

The Filter protocol is a standard CKB P2P protocol. Any peer that can connect to a node's listening port can open this protocol session and send these message types. No authentication, stake, or privilege is required. The attack is trivially scriptable.

### Recommendation

Replace `Status::ignored()` with a status code in the `4xx` ban range, or call `nc.ban_peer()` directly, consistent with how every other protocol handler in the codebase handles protocol violations:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    // remote peer should not send block filter to us without asking
    StatusCode::ProtocolMessageIsMalformed
        .with_context("received unsolicited block filter response")
}
```

This causes `should_ban()` to return `Some(BAD_MESSAGE_BAN_TIME)` and the peer will be banned by the existing dispatcher logic.

### Proof of Concept

1. Connect to a CKB full node's Filter protocol endpoint.
2. Repeatedly send a valid molecule-encoded `BlockFilters` message (or `BlockFilterHashes` / `BlockFilterCheckPoints`).
3. Observe that the node logs `"Received unexpected message from peer"` warnings but never disconnects or bans the sender.
4. The attacker can sustain this loop indefinitely, consuming the node's message-processing, logging, and metrics resources without consequence.

The root cause is confirmed at: [1](#0-0) 

with the enforcement gap confirmed by the `should_ban` logic: [2](#0-1)

### Citations

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

**File:** sync/src/filter/mod.rs (L88-114)
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

**File:** sync/src/status.rs (L164-180)
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
```
