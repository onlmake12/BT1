Looking at the full code path carefully.

**Key code to trace:**

`respond_delivered` in `connection_request.rs`: [1](#0-0) 

The guard only blocks overwrite when the existing entry is **fresh** (< 2 min). If the entry is stale (≥ 2 min old), the insert proceeds unconditionally: [2](#0-1) 

The `from` field is parsed from the message bytes with no verification against the actual sender's session/peer ID: [3](#0-2) 

The `pending_delivered` map is consumed in `ConnectionSyncProcess::execute` — it looks up by `content.from` (also unauthenticated) and passes those addresses directly to `try_nat_traversal`: [4](#0-3) 

The cleanup timer runs every 5 minutes and removes entries older than 5 minutes, leaving a **2–5 minute window** where an entry is stale enough to bypass the interval check but not yet evicted: [5](#0-4) [6](#0-5) 

The rate limiters are keyed by `(from, to, msg_item_id)` and `(session_id, msg_item_id)` — the attacker can vary `msg_item_id` to bypass both: [7](#0-6) 

---

### Title
Unauthenticated `from` field in `ConnectionRequest` allows any peer to overwrite `pending_delivered` entries, redirecting NAT traversal to attacker-controlled addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary
The `from` field in a `ConnectionRequest` P2P message is taken verbatim from the message payload with no verification that it matches the actual sender's peer ID. This allows any connected peer to insert or overwrite entries in the victim node's `pending_delivered` map for an arbitrary target peer ID. When a `ConnectionSync` subsequently arrives (or is also spoofed), the victim performs NAT traversal to attacker-controlled addresses instead of the legitimate peer's addresses.

### Finding Description
In `ConnectionRequestProcess::respond_delivered`, the victim node stores `(remote_listens, now)` into `pending_delivered` keyed by the message's `from` field:

```rust
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

The only guard against overwriting an existing entry is a 2-minute cooldown check. After that window, any connected peer can send a `ConnectionRequest` with `from` set to any arbitrary `PeerId` (e.g., a legitimate peer B that already has a valid entry), and the victim will replace the stored `listen_addrs` with attacker-controlled addresses. The `from` field is parsed from raw bytes with no cryptographic binding to the actual TCP session:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...
```

The `listen_addrs` validation only checks that any embedded peer ID matches `from` — which the attacker controls — so attacker addresses pass validation cleanly.

When `ConnectionSync` arrives (also with an unauthenticated `from` field), the victim looks up `pending_delivered.get(&content.from)` and calls `try_nat_traversal` on whatever addresses are stored there.

### Impact Explanation
- The victim node makes outbound TCP connection attempts to attacker-controlled IP addresses.
- The legitimate hole-punching attempt for the real peer B fails (its entry was overwritten), constituting a targeted DoS of the NAT traversal mechanism for a specific peer pair.
- If the attacker's endpoint accepts the connection, the victim establishes a raw P2P session with the attacker's node. While the cryptographic handshake will identify the attacker's actual peer ID (not peer B's), the victim has been induced to connect to an unintended endpoint and the intended connection to peer B is disrupted.

### Likelihood Explanation
The attacker only needs to be a connected peer (no special privilege). Peer B's peer ID is observable from the P2P network. The attacker waits ≥ 2 minutes after peer B's legitimate request, then sends a single spoofed `ConnectionRequest`. Rate limiters are bypassable by varying `msg_item_id`. The attack is deterministic and requires no brute force.

### Recommendation
Verify that the `from` field in a received `ConnectionRequest` matches the actual peer ID of the sending session. The session's peer ID is available via the peer registry — reject any message where `content.from != session_peer_id`. This eliminates the ability to spoof the `from` field entirely.

### Proof of Concept
1. Victim node V is connected to legitimate peer A and attacker peer X.
2. Peer A sends a valid `ConnectionRequest` with `from=A, to=V`; V stores `pending_delivered[A] = (A_addrs, t0)`.
3. Attacker X waits until `now - t0 >= HOLE_PUNCHING_INTERVAL` (2 minutes).
4. X sends a `ConnectionRequest` with `from=A, to=V, listen_addrs=[attacker_ip:port]`.
5. V's `respond_delivered` finds the stale entry for A, passes the interval check, and overwrites: `pending_delivered[A] = ([attacker_ip:port], now)`.
6. X (or anyone) sends a `ConnectionSync` with `from=A, to=V, route=[]`.
7. V looks up `pending_delivered[A]`, gets `[attacker_ip:port]`, and calls `try_nat_traversal(bind_addr, attacker_ip:port)`.
8. V connects to the attacker's endpoint; the legitimate connection to A is never established.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-123)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-28)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```
