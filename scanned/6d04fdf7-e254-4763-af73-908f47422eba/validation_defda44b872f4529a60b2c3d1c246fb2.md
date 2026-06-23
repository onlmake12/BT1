Looking at the full execution path across all three relevant files to trace the attack precisely.

### Title
Missing `from == self_peer_id` Guard in `ConnectionRequestProcess::execute` Allows Forced NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`ConnectionRequestProcess::execute` has no check that `content.from != self_peer_id`. An unprivileged peer can send a `ConnectionRequest` with both `from` and `to` set to the victim's own PeerId and `listen_addrs` set to attacker-controlled TCP addresses. This causes the victim to insert a self-keyed entry into `pending_delivered` mapping its own PeerId to the attacker's addresses. A subsequent `ConnectionSync` with `from = victim_peer_id` then triggers NAT traversal — outbound TCP connections — to those attacker-controlled addresses, and if successful, establishes an unauthorized raw P2P session.

---

### Finding Description

**Step 1 — Attacker sends `ConnectionRequest{from=V, to=V, listen_addrs=[attacker_ip:port]}`**

In `execute()`, the only self-referential guard is:

```rust
if content.route.contains(self_peer_id) {
    return StatusCode::Ignore...;
}
``` [1](#0-0) 

With an empty `route`, this check is bypassed. There is no guard of the form `content.from != self_peer_id`. The branch `self_peer_id == &content.to` evaluates to `true` (since `to = victim_peer_id`), so `respond_delivered(content.from, ...)` is called with `from_peer_id = victim_peer_id`. [2](#0-1) 

**Step 2 — `respond_delivered` poisons `pending_delivered`**

`respond_delivered` checks for a recent existing entry under `from_peer_id`, but on first attack there is none. It then filters `remote_listens` to TCP/IP4/IP6 addresses (attacker's addresses pass this filter) and inserts:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [3](#0-2) 

`pending_delivered` is typed as `HashMap<PeerId, (Vec<Multiaddr>, u64)>`, so the victim's own PeerId is now mapped to attacker-controlled addresses. [4](#0-3) 

**Step 3 — Attacker sends `ConnectionSync{from=V, to=V, route=[]}`**

In `ConnectionSyncProcess::execute()`, with empty route and `content.to == self_peer_id`, the code looks up:

```rust
let listens_info = self.protocol.pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
``` [5](#0-4) 

`content.from = victim_peer_id`, so it retrieves the attacker's addresses. NAT traversal is then initiated:

```rust
Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
``` [6](#0-5) 

On success, `control.raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))` is called, establishing an unauthorized raw P2P session with the attacker. [7](#0-6) 

---

### Impact Explanation

- The victim node initiates outbound TCP connections to arbitrary attacker-controlled IP:port pairs. This can be used for network-layer SSRF: the victim's IP appears to be the originator of connections to attacker infrastructure.
- If the TCP handshake succeeds, `raw_session` establishes a full P2P session, bypassing normal peer admission controls (whitelist, peer limits, etc.).
- The poisoned `pending_delivered` entry persists for up to `TIMEOUT` (5 minutes), so any legitimate `ConnectionSync` arriving during that window with `from = victim_peer_id` also triggers traversal to attacker addresses. [8](#0-7) 

---

### Likelihood Explanation

- The attacker only needs a standard P2P connection to the victim — no privileged access, no leaked keys.
- The victim's PeerId is publicly observable from the P2P network.
- The attack requires exactly two messages (`ConnectionRequest` + `ConnectionSync`) and succeeds on first attempt (no brute force).
- The rate limiter key is `(from, to, msg_item_id)` = `(victim_peer_id, victim_peer_id, id)`, which only throttles repeated identical messages, not the initial one. [9](#0-8) 

---

### Recommendation

Add an explicit guard at the top of `execute()` rejecting any `ConnectionRequest` where `content.from == self_peer_id`:

```rust
let self_peer_id = self.protocol.network_state.local_peer_id();
if &content.from == self_peer_id {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id must not equal self");
}
```

This should be placed before the `content.to == self_peer_id` branch. Similarly, a guard `content.from != content.to` should be added to reject trivially self-referential requests.

---

### Proof of Concept

```
1. Attacker A connects to victim V (standard P2P handshake).
2. A sends HolePunchingMessage::ConnectionRequest {
       from: V.peer_id,
       to:   V.peer_id,
       listen_addrs: [/ip4/1.2.3.4/tcp/9999/p2p/<V.peer_id>],
       route: [],
       max_hops: 6,
   }
3. V.execute() → self_peer_id == content.to → respond_delivered(V.peer_id, attacker_addr)
   → pending_delivered[V.peer_id] = ([/ip4/1.2.3.4/tcp/9999/...], now)
4. A sends HolePunchingMessage::ConnectionSync {
       from: V.peer_id,
       to:   V.peer_id,
       route: [],
   }
5. V.execute() → content.to == self_peer_id
   → pending_delivered.get(V.peer_id) = [/ip4/1.2.3.4/tcp/9999/...]
   → try_nat_traversal → TCP connect to 1.2.3.4:9999
   → raw_session established with attacker
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-130)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-123)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L154-160)
```rust
                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
```
