### Title
Unauthenticated `from` Field in `ConnectionRequest` Allows Cache Poisoning of `pending_delivered`, Suppressing Legitimate Hole-Punching Responses for 2 Minutes — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged connected peer can send a `ConnectionRequest` message with an arbitrary `from` field set to any target peer ID. The victim node's `respond_delivered` function uses this unauthenticated `from` value as the key into `pending_delivered`. A single spoofed message poisons that map entry, causing all legitimate hole-punching requests from the impersonated peer to be silently dropped for the full `HOLE_PUNCHING_INTERVAL` (2 minutes). The attacker can sustain the block indefinitely by re-sending every ~2 minutes.

---

### Finding Description

The `ConnectionRequest` wire message carries a `from: Bytes` field that is fully attacker-controlled; nothing in the protocol binds it to the actual P2P session identity of the sender.

In `execute()`, when `self_peer_id == &content.to`, the node calls `respond_delivered(content.from, ...)`: [1](#0-0) 

Inside `respond_delivered`, the deduplication guard keys on `from_peer_id` — the value taken verbatim from the message: [2](#0-1) 

If no prior entry exists, the function sends the response to the **actual session** (the attacker), then writes the spoofed `from_peer_id` into `pending_delivered` with the current timestamp: [3](#0-2) 

`HOLE_PUNCHING_INTERVAL` is 2 minutes: [4](#0-3) 

`pending_delivered` is keyed by `PeerId` with no session binding: [5](#0-4) 

The existing rate limiter does **not** prevent this attack — it keys on `(content.from, content.to, msg_item_id)`, all of which the attacker controls: [6](#0-5) 

---

### Impact Explanation

- Any connected peer can poison `pending_delivered` for an arbitrary `(from, to)` pair.
- The legitimate peer's subsequent `ConnectionRequest` hits the deduplication guard and returns `StatusCode::Ignore`, silently discarding the request with no error to the legitimate sender.
- The attacker re-sends every ~2 minutes to maintain the block indefinitely.
- The `connection_sync.rs` passive NAT-traversal path also depends on `pending_delivered` to retrieve listen addresses; a poisoned entry there supplies the attacker's addresses instead of the legitimate peer's: [7](#0-6) 

- Net effect: targeted NAT-traversal pairs are permanently denied hole-punching connectivity. Nodes behind NAT that rely on this path to reach peers cannot establish those connections, degrading network topology. Consensus deviation via network partition is a plausible downstream consequence for heavily NAT-ed deployments, though it is indirect.

---

### Likelihood Explanation

- Requires only a standard, unprivileged P2P connection to the victim — no special privileges, no key material, no majority hashpower.
- The spoofed `from` field requires no cryptographic forgery; it is plain bytes with no signature or session binding.
- The attacker needs to know (or enumerate) the target legitimate peer ID, which is publicly observable from the P2P peer exchange.
- Sustaining the attack costs one message per 2-minute window per targeted pair.

---

### Recommendation

Bind the `from` field to the actual session identity. In `execute()`, replace `content.from` with the peer ID derived from the authenticated session (i.e., look up the session's peer ID from `peer_registry` using `self.peer`), and reject any message where `content.from` does not match the session's authenticated peer ID. This ensures `pending_delivered` can only be written by the peer that actually owns the `from` identity.

---

### Proof of Concept

```
1. Attacker establishes a valid P2P session with victim V.
2. Attacker sends ConnectionRequest {
       from = <legitimate_peer_id L>,
       to   = <victim V>,
       listen_addrs = [<valid TCP addr>],
       max_hops = 1,
       route = [],
   }
3. V's respond_delivered:
   - pending_delivered.get(&L) → None  (first time)
   - sends ConnectionRequestDelivered back to attacker's session
   - inserts pending_delivered[L] = (attacker_addrs, now)
4. Legitimate peer L sends its own ConnectionRequest { from=L, to=V, ... }
5. V's respond_delivered:
   - pending_delivered.get(&L) → Some((_, t))
   - now - t < HOLE_PUNCHING_INTERVAL (120_000 ms)
   - returns StatusCode::Ignore  ← legitimate request silently dropped
6. Attacker repeats step 2 every ~119 seconds to maintain the block.
```

### Citations

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
