The code is fully readable. Let me analyze the exact call chain.

The full call chain is now confirmed. Here is the assessment:

---

### Title
Missing Ban for Unsolicited Filter Response Messages Allows Indefinite Peer Misbehavior — (`sync/src/filter/mod.rs`)

### Summary

An unprivileged remote peer can send unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages to any filter-enabled CKB node. The handler explicitly acknowledges this is a protocol violation but returns `Status::ignored()` instead of a ban status, and the `should_ban()` logic never triggers for that status code. The peer is never disconnected or banned, allowing continuous unsolicited message flooding with no enforcement consequence.

### Finding Description

The call chain is fully traceable:

**Step 1 — Entry point:** `CKBProtocolHandler::received` parses the raw bytes. [1](#0-0) 

Malformed messages are banned here. Well-formed messages proceed to `process`.

**Step 2 — Dispatch:** `process` calls `try_process` and then checks `status.should_ban()`. [2](#0-1) 

**Step 3 — The missing ban:** In `try_process`, the three unsolicited response message types (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) hit this arm: [3](#0-2) 

The comment at line 59 explicitly says `// TODO: ban remote peer`. The function returns `Status::ignored()`.

**Step 4 — Why `should_ban()` returns `None`:** `Status::ignored()` uses `StatusCode::Ignored = 101`. [4](#0-3) 

`should_ban()` only returns a ban duration for codes in the `400..500` range: [5](#0-4) 

Code `101` is not in that range, so `should_ban()` returns `None`, and `nc.ban_peer(...)` is never called.

### Impact Explanation

A peer can maintain a persistent connection and send a continuous stream of well-formed unsolicited response messages. Each message causes:
- A full `from_compatible_slice` parse pass
- A `warn_target!` log write

The node has no mechanism to disconnect or penalize the peer for this behavior. The impact is real but bounded: per-message cost is low (parse + log), so this is not a catastrophic CPU/memory exhaustion. The concrete harm is:
- Log flooding (warn log per message, unbounded)
- Minor sustained CPU overhead from parsing
- The peer permanently occupies a connection slot without being evicted for this class of misbehavior

The claim of "network-wide congestion" is overstated — the per-message work is trivial and the real bottleneck is network bandwidth, not handler CPU. However, the missing enforcement is a genuine protocol-level gap: a peer that violates the filter protocol by sending unsolicited responses faces zero consequence.

### Likelihood Explanation

The path requires only: (1) the Filter protocol is enabled on the target node, (2) the attacker connects as a peer, (3) the attacker sends any of the three unsolicited response message types. No privileges, no PoW, no keys. The TODO comment confirms this is a known unimplemented enforcement point.

### Recommendation

Replace `Status::ignored()` in the unsolicited-response arm of `try_process` with a ban status, e.g. `StatusCode::ProtocolMessageIsMalformed.with_context("unsolicited block filter response")`. This will cause `should_ban()` to return `Some(BAD_MESSAGE_BAN_TIME)` and trigger `nc.ban_peer(...)` in `process`, removing the TODO. [3](#0-2) [6](#0-5) 

### Proof of Concept

1. Enable the Filter protocol on a CKB node.
2. Connect as a peer.
3. Continuously send well-formed `BlockFilters` messages (valid molecule encoding, no request preceding them).
4. Observe: the node emits `warn_target!` logs indefinitely, never calls `ban_peer`, and the connection is never terminated for this behavior.
5. Confirm: `StatusCode::Ignored = 101` is outside `400..500`, so `should_ban()` always returns `None` for these messages.

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

**File:** sync/src/status.rs (L74-74)
```rust
    ProtocolMessageIsMalformed = 400,
```

**File:** sync/src/status.rs (L155-157)
```rust
    pub fn ignored() -> Self {
        Self::new::<&str>(StatusCode::Ignored, None)
    }
```

**File:** sync/src/status.rs (L165-168)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
```
