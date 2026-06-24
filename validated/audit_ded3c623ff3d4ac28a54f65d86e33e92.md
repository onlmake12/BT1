All code references check out exactly. Let me verify the `UnexpectedProtocolMessage` status code in the light-client server to confirm the comparison is accurate.

Audit Report

## Title
Missing Peer Ban for Unexpected Response Messages in `BlockFilter` Protocol Handler — (File: `sync/src/filter/mod.rs`)

## Summary
The `BlockFilter` protocol handler returns `Status::ignored()` (code 101) when a peer sends response-type messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) that should never flow from a peer to a full node. Because `should_ban()` only triggers on 4xx-range codes, the peer is never banned or disconnected and can flood the node with unsolicited messages indefinitely. The code itself marks this gap with an explicit `// TODO: ban remote peer` comment.

## Finding Description
In `sync/src/filter/mod.rs` lines 55–66, the three response-type message variants fall into a catch-all arm that logs a warning and returns `Status::ignored()`:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    // remote peer should not send block filter to us without asking
    // TODO: ban remote peer
    warn_target!(...);
    Status::ignored()   // StatusCode::Ignored = 101
}
```

The `process()` dispatcher at lines 88–97 calls `nc.ban_peer()` only when `status.should_ban()` returns `Some`. In `sync/src/status.rs` lines 165–168, `should_ban()` returns `None` for any code outside `400..500`:

```rust
pub fn should_ban(&self) -> Option<Duration> {
    if !(400..500).contains(&(self.code as u16)) {
        return None;
    }
```

`StatusCode::Ignored = 101` is outside that range, so no ban is issued. The peer remains connected and can repeat the flood indefinitely.

`BlockFilter` carries no rate limiter (lines 22–25), unlike `Relayer` which has an explicit `RateLimiter<(PeerIndex, u32)>` enforcing 30 req/s per peer. The correct pattern already exists in `util/light-client-protocol-server/src/lib.rs` line 123, where unexpected messages return `StatusCode::UnexpectedProtocolMessage` (= 401), which triggers a ban.

The Filter protocol is enabled by default on full nodes: `resource/ckb.toml` line 112 includes `"Filter"` in `support_protocols`, and `util/launcher/src/lib.rs` lines 443–456 register `BlockFilter` when that config entry is present.

## Impact Explanation
Each unsolicited message causes: (1) full Molecule deserialization of up to 2 MB (`max_frame_length` for Filter), (2) a `warn_target!` log write, and (3) a `metric_ckb_message_bytes` counter update — all with no enforcement response. A sustained flood degrades the node's CPU, log I/O, and its ability to serve legitimate light-client requests. This matches **Low (501–2000 points): Any other important performance improvements for CKB**. The impact is bounded to a single node and does not rise to node crash or network-wide congestion without additional amplification.

## Likelihood Explanation
The Filter protocol (ID 121) is negotiated during standard peer connection with no privilege requirement. Constructing a valid Molecule-encoded `BlockFilters` message requires only knowledge of the public schema. Any peer that connects to a full node's P2P port can trigger this path immediately after protocol negotiation. No keys, stake, or special role are needed.

## Recommendation
Replace `Status::ignored()` with a ban-triggering 4xx status code in the unexpected-message arm, consistent with `util/light-client-protocol-server/src/lib.rs` line 123:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    StatusCode::ProtocolMessageIsMalformed
        .with_context("unexpected response message from peer")
}
```

This causes `process()` to call `nc.ban_peer(peer, BAD_MESSAGE_BAN_TIME, ...)`, disconnecting and temporarily banning the offending peer. Additionally, consider adding a per-peer rate limiter to `BlockFilter` as `Relayer` does.

## Proof of Concept
1. Connect to a full node's P2P port and negotiate `SupportProtocols::Filter` (protocol ID 121).
2. Construct a minimal valid Molecule-encoded `BlockFilterMessage` wrapping an empty `BlockFilters` payload (`start_number = 0`, empty `block_hashes` and `filters` vectors).
3. Send this message in a tight loop.
4. Observe: the node logs a `warn` for each message, records a metric, and never bans or disconnects the sender. The peer connection remains open indefinitely. Confirm by checking node logs for repeated `"Received unexpected message from peer"` entries and verifying the peer index remains connected throughout.