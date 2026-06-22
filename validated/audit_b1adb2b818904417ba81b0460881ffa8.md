### Title
Missing Peer Ban for Unexpected Response Messages in `BlockFilter` Protocol Handler — (File: `sync/src/filter/mod.rs`)

---

### Summary

The `BlockFilter` protocol handler on full nodes silently ignores response-type messages (`BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints`) sent by a connected peer without banning or rate-limiting that peer. An unprivileged peer can exploit this to send an unbounded stream of unsolicited response messages to a full node, consuming CPU and I/O resources indefinitely with no enforcement consequence. The code itself acknowledges the gap with an explicit `// TODO: ban remote peer` comment.

---

### Finding Description

In `sync/src/filter/mod.rs`, the `BlockFilter::try_process` function dispatches on the six variants of `packed::BlockFilterMessageUnionReader`. Three variants are request-type messages that a full node legitimately handles (`GetBlockFilters`, `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`). The remaining three — `BlockFilters`, `BlockFilterHashes`, `BlockFilterCheckPoints` — are response-type messages that should only flow from a full node *to* a light client, never in the reverse direction.

When a peer sends one of these response-type messages to a full node, the handler falls into this arm:

```rust
// sync/src/filter/mod.rs, lines 55-66
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

`Status::ignored()` returns `StatusCode::Ignored = 101`. The `should_ban()` method in `sync/src/status.rs` only triggers a ban for 4xx-range codes:

```rust
// sync/src/status.rs, lines 165-179
pub fn should_ban(&self) -> Option<Duration> {
    if !(400..500).contains(&(self.code as u16)) {
        return None;
    }
    // ...
}
```

Because `Ignored(101)` is outside the 4xx range, the `process()` dispatcher never calls `nc.ban_peer()`. The peer is not disconnected, not banned, and not rate-limited — it can repeat this indefinitely.

Compounding this, the `BlockFilter` struct carries **no rate limiter**:

```rust
// sync/src/filter/mod.rs, lines 22-25
pub struct BlockFilter {
    shared: Arc<SyncShared>,
}
```

This contrasts with `Relayer`, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and enforces 30 req/s per peer per message type, and `HolePunching`, which has two separate rate limiters. `BlockFilter` has neither.

The correct pattern is already used in `util/light-client-protocol-server/src/lib.rs` at line 123, where unexpected messages return a ban-triggering status code (`StatusCode::UnexpectedProtocolMessage`) rather than `Status::ignored()`.

---

### Impact Explanation

A malicious peer that connects to a full node and negotiates the Filter protocol (protocol ID 121) can send a continuous flood of `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` messages. Each message:

1. Is fully deserialized by `packed::BlockFilterMessageReader::from_compatible_slice` (CPU cost).
2. Triggers a `warn_target!` log write (I/O cost).
3. Records a metric via `metric_ckb_message_bytes` (CPU/memory cost).
4. Returns without banning or disconnecting the peer.

Because there is no rate limiter and no ban, the peer occupies a connection slot and can sustain this flood for the entire duration of the connection, degrading the node's ability to serve legitimate light-client requests and consuming node resources (CPU, I/O, log storage). This is a resource-exhaustion path reachable by any unprivileged peer.

---

### Likelihood Explanation

The Filter protocol (`SupportProtocols::Filter`, protocol ID 121) is a standard protocol negotiated during peer connection. Any peer that connects to a full node with filter support enabled can negotiate this protocol without any privilege. Constructing a valid `BlockFilters`, `BlockFilterHashes`, or `BlockFilterCheckPoints` Molecule-encoded message requires only knowledge of the public schema. The attack requires no keys, no stake, and no special role — only a TCP connection to the node's P2P port.

---

### Recommendation

Replace `Status::ignored()` with a ban-triggering status code for the unexpected response-message arm, consistent with how other protocol handlers treat protocol violations. For example:

```rust
packed::BlockFilterMessageUnionReader::BlockFilters(_)
| packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
| packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
    StatusCode::ProtocolMessageIsMalformed
        .with_context("unexpected response message from peer")
}
```

This causes `process()` to call `nc.ban_peer(peer, BAD_MESSAGE_BAN_TIME, ...)`, which disconnects and temporarily bans the offending peer, consistent with how malformed messages are handled throughout the codebase.

Additionally, consider adding a per-peer rate limiter to `BlockFilter` (as `Relayer` and `HolePunching` do) to bound the processing cost even for well-formed request messages.

---

### Proof of Concept

1. Connect to a full node's P2P port and negotiate `SupportProtocols::Filter` (protocol ID 121).
2. Construct a valid Molecule-encoded `BlockFilterMessage` wrapping a `BlockFilters` payload (e.g., an empty `BlockFilters` with `start_number = 0` and empty `block_hashes`/`filters` vectors).
3. Send this message in a tight loop.
4. Observe: the node logs a `warn` for each message, records a metric, and never bans or disconnects the sender. The peer connection remains open indefinitely and the node's CPU and log I/O are consumed at the rate the attacker sends messages.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** sync/src/filter/mod.rs (L22-25)
```rust
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

**File:** sync/src/status.rs (L165-179)
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
