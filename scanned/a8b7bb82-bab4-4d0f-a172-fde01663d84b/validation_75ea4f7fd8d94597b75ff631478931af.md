### Title
Unsolicited Block Filter Response Messages Silently Ignored Without Peer Penalty — (`sync/src/filter/mod.rs`)

---

### Summary

In `sync/src/filter/mod.rs`, when a remote peer sends unsolicited block filter response messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) — which should only arrive in response to explicit local requests — the handler detects the protocol violation but silently returns `Status::ignored()` instead of banning the peer. The code itself contains a `// TODO: ban remote peer` comment acknowledging the gap. This is a direct analog to the "fail loudly" class: a condition is checked, the check fails, but the caller receives no actionable error and the violating peer faces no consequence.

---

### Finding Description

In `sync/src/filter/mod.rs`, the `try_process` function dispatches incoming `BlockFilterMessage` variants: [1](#0-0) 

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

The three message types (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) are response-only messages that a full node should only receive after it has explicitly requested them. A peer sending them unsolicited is violating the protocol. The code correctly identifies this (`remote peer should not send block filter to us without asking`) and even acknowledges the correct remediation (`TODO: ban remote peer`), but instead returns `Status::ignored()`.

The `process` wrapper that calls `try_process` then evaluates the returned status: [2](#0-1) 

`Status::ignored()` does not trigger `should_ban()` or `should_warn()` at the ban level, so `nc.ban_peer(...)` is never called. The peer is not disconnected, not penalized, and is free to repeat the behavior indefinitely.

---

### Impact Explanation

An unprivileged remote peer can send an unbounded stream of unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages. Each message:

1. Is deserialized and dispatched through the full protocol handler path.
2. Triggers a `warn_target!` log write.
3. Returns without any peer penalty.

Because the peer is never banned or disconnected, the attacker retains the connection and can repeat the pattern at will. The practical effect is:

- **Protocol enforcement bypass**: The filter sub-protocol's request/response invariant is unenforced. A peer that should be banned for misbehavior is instead silently tolerated.
- **Sustained resource consumption**: Log I/O, message deserialization, and handler dispatch are repeated for every unsolicited message with no back-pressure mechanism against the sender.
- **Peer slot exhaustion amplification**: A misbehaving peer that would normally be evicted retains its connection slot, potentially displacing honest peers.

---

### Likelihood Explanation

The entry path requires only a standard P2P connection — no privileged access, no keys, no majority hashpower. Any peer that has completed the handshake and negotiated the `Filter` protocol can send these message types. The `BlockFilterMessage` encoding is public (Molecule schema), so crafting the messages is trivial. The `// TODO: ban remote peer` comment confirms the developers are aware this path is reachable and unguarded.

---

### Recommendation

Replace `Status::ignored()` in the unsolicited-response arm with a ban-worthy status, consistent with how other protocol violations are handled elsewhere in the sync layer (e.g., `StatusCode::UnexpectedMessage` or equivalent):

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    StatusCode::UnexpectedMessage
        .with_context("received unsolicited block filter response")
}
```

This ensures `process()` calls `nc.ban_peer(peer, ban_time, ...)`, terminating the connection and preventing repeated abuse. The fix is consistent with the `// TODO: ban remote peer` note already present in the code.

---

### Proof of Concept

1. Establish a P2P connection to a CKB full node and negotiate the `Filter` protocol (`SupportProtocols::Filter`).
2. Without sending any `GetBlockFilters`, `GetBlockFilterHashes`, or `GetBlockFilterCheckPoints` request, send a well-formed `BlockFilters` (or `BlockFilterHashes` / `BlockFilterCheckPoints`) Molecule-encoded message.
3. Observe: the node logs a `warn` entry (`"Received unexpected message from peer"`) but does **not** disconnect or ban the peer.
4. Repeat step 2 in a tight loop. The connection remains open, the node continues processing and logging each message, and no ban is applied — confirming the silent-failure behavior. [1](#0-0)

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
