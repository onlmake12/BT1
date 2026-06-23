### Title
Missing Peer Ban for Unsolicited Block Filter Messages Leaves Node Unprotected Against Spam — (File: `sync/src/filter/mod.rs`)

### Summary

The `BlockFilter` P2P protocol handler in `sync/src/filter/mod.rs` explicitly acknowledges via a `// TODO: ban remote peer` comment that peers sending unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages should be banned, but the ban is never executed. The handler returns `Status::ignored()` instead of a ban-carrying status, so the protective action is permanently a no-op. Any unprivileged peer can flood the node with large unsolicited filter messages indefinitely without consequence.

### Finding Description

In `sync/src/filter/mod.rs`, the `try_process` function dispatches incoming `BlockFilterMessage` variants. Three of those variants — `BlockFilters`, `BlockFilterHashes`, and `BlockFilterCheckPoints` — are **response** messages that a peer should only send after the local node has explicitly requested them. When a peer sends them unsolicited, the handler reaches this branch:

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
``` [1](#0-0) 

The caller of `try_process` only bans a peer when `status.should_ban()` returns `Some(ban_time)`:

```rust
if let Some(ban_time) = status.should_ban() {
    nc.ban_peer(peer, ban_time, status.to_string());
}
``` [2](#0-1) 

`Status::ignored()` never satisfies `should_ban()`, so the peer is never banned. The protective action — the analog of `removeConnectorAdmin` in the original report — is permanently unreachable because the implementation was never completed.

The `Filter` protocol is enabled by default in the node's `support_protocols` list: [3](#0-2) 

A `BlockFilters` response message can carry up to ~1.8 MB of filter data per batch (the size cap is enforced only on the *sender* side in `get_block_filters_process.rs`): [4](#0-3) 

A malicious peer is not subject to that cap when crafting its own messages.

### Impact Explanation

A malicious peer can connect to any CKB node that has the `Filter` protocol enabled (the default) and repeatedly send large, unsolicited `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages. Because the node never bans the peer, the attack loop is unbounded. Each message is deserialized and dispatched through the handler, consuming CPU and bandwidth. The node has no mechanism to terminate the connection based on this misbehavior alone. This constitutes a realistic resource-exhaustion (bandwidth/CPU) denial-of-service path reachable by any unprivileged network peer.

**Impact: 3 / 10** — resource exhaustion; no consensus or state corruption.

### Likelihood Explanation

The `Filter` protocol is on by default. Any peer that can establish a TCP connection to the node's P2P port can exploit this. No privileged access, leaked keys, or majority hashpower is required. The attacker only needs to craft and send valid Molecule-encoded `BlockFilterMessage` packets with the unsolicited response variants.

**Likelihood: 3 / 10** — trivially reachable by any network peer; requires sustained connection.

### Recommendation

Replace `Status::ignored()` in the unsolicited-filter-message branch with a ban-carrying status (e.g., `StatusCode::UnexpectedMessage.with_context(...)`) so that `should_ban()` returns a non-zero duration and `nc.ban_peer(...)` is actually invoked. This mirrors the pattern already used elsewhere in the relay and sync handlers when peers send protocol-violating messages.

### Proof of Concept

1. Connect to a CKB node (default port 8115) using the `Filter` sub-protocol (`SupportProtocols::Filter`).
2. Without sending any `GetBlockFilters` / `GetBlockFilterHashes` / `GetBlockFilterCheckPoints` request, immediately send a `BlockFilters` message (Molecule-encoded, `start_number = 0`, with arbitrary `block_hashes` and `filters` vectors).
3. Observe: the node logs `"Received unexpected message from peer"` but does **not** disconnect or ban the peer.
4. Repeat step 2 in a tight loop. The peer remains connected and the node continues processing each message, consuming CPU and bandwidth, with no rate-limiting or ban applied.

The `try_process` dispatch path that is hit is exactly: [1](#0-0) 

and the outer `process` function confirms no ban fires because `status.should_ban()` is `None` for `Status::ignored()`: [2](#0-1)

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

**File:** sync/src/filter/mod.rs (L88-98)
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
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
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
