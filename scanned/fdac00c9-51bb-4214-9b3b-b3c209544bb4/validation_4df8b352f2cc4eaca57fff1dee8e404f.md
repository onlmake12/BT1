### Title
Per-Session Rate Limiter Bypass via Reconnection in Relay and HolePunching Protocols — (`sync/src/relayer/mod.rs`, `network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The Relay and HolePunching protocol rate limiters are keyed by `PeerIndex` (a per-session identifier). When a peer disconnects and reconnects, it receives a new `PeerIndex`, resetting its rate-limit bucket entirely. An unprivileged inbound peer can therefore bypass the 30 req/sec cap on relay messages by cycling connections, enabling unbounded message flooding against a CKB node.

---

### Finding Description

Both the `Relayer` and `HolePunching` protocol handlers maintain in-process rate limiters keyed by `(PeerIndex, message_item_id)`:

**Relayer** — `sync/src/relayer/mod.rs` [1](#0-0) 

The rate limiter is checked per-message: [2](#0-1) 

On disconnect, only `retain_recent()` is called — it prunes stale entries globally but does **not** remove the disconnecting peer's specific bucket: [3](#0-2) 

**HolePunching** — `network/src/protocols/hole_punching/mod.rs` [4](#0-3) 

Same pattern on disconnect: [5](#0-4) 

`PeerIndex` is a session-scoped integer assigned at connection time. When a peer disconnects and reconnects, the tentacle P2P layer assigns a new `PeerIndex`. The old rate-limit entry for the previous `PeerIndex` is orphaned and eventually pruned by `retain_recent()`, but the **new** connection starts with a completely fresh, empty bucket.

The ban mechanism operates at the IP level and is only triggered by malformed messages or protocol violations — **not** by `TooManyRequests`. A peer that hits the rate limit is silently dropped with no ban: [6](#0-5) 

The IP-level ban list correctly enforces bans across reconnections: [7](#0-6) 

But rate-limit state is never stored in the ban list or any IP-keyed structure.

---

### Impact Explanation

An attacker controlling one or more inbound connections can:

1. Connect to the victim CKB node.
2. Send relay messages (`GetRelayTransactions`, `RelayTransactionHashes`, `GetBlockTransactions`, `GetBlockProposal`, `BlockProposal`) at 30 req/sec until rate-limited.
3. Immediately disconnect and reconnect, obtaining a new `PeerIndex` and a fresh rate-limit bucket.
4. Repeat indefinitely.

This allows sustained message flooding at rates far exceeding the intended 30 req/sec cap, consuming victim CPU (message parsing, deserialization, tx-pool lookups) and memory (queued work). The rate limiter — the only soft-throttle defense for these message types — provides a false sense of security, exactly analogous to the WAF cookie-scoped block.

---

### Likelihood Explanation

Any unprivileged peer reachable over TCP can exploit this. No special privileges, keys, or majority hashpower are required. The attacker only needs to be able to open inbound connections to the target node (the default CKB configuration accepts inbound peers). Reconnection overhead (TCP + secio handshake) is on the order of tens of milliseconds, making the effective bypass rate high enough to be practically useful for flooding.

---

### Recommendation

- Key the rate limiter by the peer's **IP address** (extracted from `connected_addr`) rather than by `PeerIndex`. This mirrors how the ban list works and survives reconnection.
- Alternatively, on `disconnected`, explicitly remove the disconnecting peer's entries from the rate limiter hashmap using the known `PeerIndex`, and maintain a separate IP-keyed counter that persists across reconnections.
- Consider escalating repeated rate-limit violations from the same IP to a temporary IP-level ban (analogous to the WAF report's recommendation of progressive escalation: per-session throttle → IP-level ban).

---

### Proof of Concept

```
Attacker peer (IP: 1.2.3.4, PeerIndex=7):
  → sends 30 GetRelayTransactions/sec → rate limited (TooManyRequests, no ban)

Attacker disconnects and reconnects (same IP: 1.2.3.4, new PeerIndex=8):
  → rate_limiter has no entry for (PeerIndex=8, item_id)
  → sends 30 more GetRelayTransactions/sec → accepted ✅

Repeat indefinitely → unbounded relay message flood
```

The root cause is in `Relayer::new()` where the limiter is initialized with `RateLimiter::hashmap(quota)` keyed on `(PeerIndex, u32)`: [8](#0-7) 

and in `HolePunching::new()`: [9](#0-8)

### Citations

**File:** sync/src/relayer/mod.rs (L63-82)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
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

**File:** sync/src/relayer/mod.rs (L923-935)
```rust
    async fn disconnected(
        &mut self,
        _nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
    ) {
        info_target!(
            crate::LOG_TARGET_RELAY,
            "RelayProtocol.disconnected peer={}",
            peer_index
        );
        // Retains all keys in the rate limiter that were used recently enough.
        self.rate_limiter.retain_recent();
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L45-47)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L248-257)
```rust
    pub(crate) fn new(network_state: Arc<NetworkState>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```
