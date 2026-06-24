Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest` Enables Cache Poisoning of `pending_delivered`, Blocking Hole-Punching for Targeted Peers — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
The `ConnectionRequest` message's `from` field is parsed directly from attacker-supplied bytes with no binding to the authenticated session identity. When the victim node is the `to` target, `respond_delivered` uses this unauthenticated `from` value as the key into `pending_delivered`. A single spoofed message poisons that map entry, causing all legitimate hole-punching requests from the impersonated peer to be silently dropped for the full `HOLE_PUNCHING_INTERVAL` (2 minutes), renewable indefinitely at negligible cost.

## Finding Description

**Root cause:** In `execute()`, `content.from` is taken verbatim from the wire message with no check against the actual session's peer ID: [1](#0-0) 

The `peer` field (`PeerIndex`) on `ConnectionRequestProcess` holds the real session identity but is never used to validate `content.from`: [2](#0-1) 

**Deduplication guard:** Inside `respond_delivered`, the guard keys exclusively on `from_peer_id` — the value taken verbatim from the message: [3](#0-2) 

**Poison write:** After sending the response to the attacker's actual session (`self.peer`), the spoofed `from_peer_id` is written into `pending_delivered` with the current timestamp: [4](#0-3) 

**Map structure — no session binding:** `pending_delivered` is a plain `HashMap<PeerId, PendingDeliveredInfo>` with no association to any session: [5](#0-4) 

**`HOLE_PUNCHING_INTERVAL`:** The deduplication window is 2 minutes: [6](#0-5) 

**Existing rate limiter is insufficient:** The `forward_rate_limiter` keys on `(content.from, content.to, msg_item_id)` — all attacker-controlled. It allows 1 request/second per key, but the attack only needs 1 message per 2-minute window, so the limiter never fires: [7](#0-6) 

**Secondary impact — `connection_sync.rs`:** The passive NAT-traversal path reads `pending_delivered` keyed on `content.from` (also unauthenticated) to obtain listen addresses for the actual TCP hole-punch attempt. A poisoned entry supplies the attacker's addresses instead of the legitimate peer's: [8](#0-7) 

## Impact Explanation

An unprivileged attacker with a single P2P connection can permanently suppress hole-punching connectivity for any targeted `(from, to)` peer pair. For nodes behind NAT that depend on this protocol to join the network, systematic targeting of multiple pairs degrades network topology. At scale — targeting many NAT-ed nodes — this constitutes a low-cost network disruption matching **High: Vulnerabilities or bad designs which could cause CKB network congestion/disruption with few costs**. The secondary `connection_sync` path additionally misdirects NAT traversal attempts to attacker-controlled addresses, compounding the connectivity denial.

## Likelihood Explanation

- Requires only a standard, unprivileged P2P connection — no key material, no special privileges.
- Target peer IDs are publicly observable via peer exchange.
- Sustaining the block costs one message per 2-minute window per targeted pair — negligible bandwidth.
- No cryptographic forgery required; `from` is plain bytes with no signature or session binding.

## Recommendation

In `execute()`, replace `content.from` with the peer ID derived from the authenticated session. Look up the session's peer ID from `peer_registry` using `self.peer` (the `PeerIndex` already available on `ConnectionRequestProcess`), and reject any message where `content.from` does not match the session's authenticated peer ID. This ensures `pending_delivered` can only be written by the peer that actually owns the `from` identity. Apply the same fix to `ConnectionSyncProcess` for the `connection_sync.rs` path.

## Proof of Concept

```
1. Attacker A establishes a valid P2P session with victim V.
2. A sends ConnectionRequest {
       from = <legitimate_peer_id L>,   // spoofed
       to   = <victim V>,
       listen_addrs = [<attacker TCP addr>],
       max_hops = 1,
       route = [],
   }
3. V's respond_delivered:
   - pending_delivered.get(&L) → None  (first time)
   - sends ConnectionRequestDelivered back to A's session (self.peer)
   - inserts pending_delivered[L] = (attacker_addrs, now)
4. Legitimate peer L sends its own ConnectionRequest { from=L, to=V, ... }
5. V's respond_delivered:
   - pending_delivered.get(&L) → Some((_, t))
   - now - t < HOLE_PUNCHING_INTERVAL (120_000 ms)
   - returns StatusCode::Ignore  ← legitimate request silently dropped
6. A repeats step 2 every ~119 seconds to maintain the block indefinitely.
7. If L also sends a ConnectionSync, V reads pending_delivered[L] and
   attempts NAT traversal to A's addresses, not L's.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```
