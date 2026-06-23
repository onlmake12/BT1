The code confirms the described vulnerability. Let me trace through the exact execution path.

### Title
Stale `pending_delivered` Entries Persist After Peer Disconnect, Enabling Attacker-Directed NAT Traversal — (`network/src/protocols/hole_punching/mod.rs`, `connection_request.rs`, `connection_sync.rs`)

---

### Summary

`HolePunching::disconnected()` does not remove `pending_delivered` entries for the disconnecting peer. An unprivileged remote peer can populate `pending_delivered[attacker_peer_id]` with attacker-controlled listen addresses, disconnect, and then (from any session) send a `ConnectionSync` with `from=attacker_peer_id` to cause the victim node to initiate NAT traversal TCP connections to those stale attacker-controlled addresses — up to 5 minutes after the original connection.

---

### Finding Description

**Step 1 — Populate `pending_delivered`.**

When the victim node is the `to` target of a `ConnectionRequest`, `ConnectionRequestProcess::respond_delivered()` inserts the attacker's listen addresses into `pending_delivered`: [1](#0-0) 

The key is `from_peer_id` (attacker-controlled `PeerId`), the value is `(remote_listens, now)` where `remote_listens` are attacker-supplied addresses.

**Step 2 — Disconnect without cleanup.**

`HolePunching::disconnected()` only calls the two rate limiter housekeeping methods and does nothing to `pending_delivered`: [2](#0-1) 

The entry for `attacker_peer_id` remains in the map.

**Step 3 — Stale entry lifetime.**

The only cleanup is in `notify()`, which runs every `CHECK_INTERVAL = 5 minutes` and evicts entries older than `TIMEOUT = 5 minutes`: [3](#0-2) [4](#0-3) 

An entry inserted just after a `notify()` tick survives until the next tick (~5 minutes later).

**Step 4 — Unauthenticated `ConnectionSync` triggers NAT traversal to stale addresses.**

`ConnectionSyncProcess::execute()` looks up `pending_delivered` by `content.from`, which is an attacker-supplied field with no verification that the actual sender of the `ConnectionSync` message is the peer identified by `content.from`: [5](#0-4) 

If the stale entry is found, the victim spawns outbound TCP connection tasks to every address in the stale entry: [6](#0-5) 

On success, `raw_session()` is called, establishing a new inbound session from the victim's perspective to the attacker-controlled address: [7](#0-6) 

---

### Impact Explanation

- The victim node initiates outbound TCP connections to attacker-controlled addresses. If the attacker accepts, a `raw_session` is registered as an inbound session on the victim, potentially bypassing IP-based ban checks (which typically apply to inbound connections from the remote, not outbound connections initiated by the local node).
- Because `content.from` is not authenticated against the actual sender, any currently-connected peer can trigger NAT traversal to any `PeerId` that has a stale `pending_delivered` entry — not just the original attacker.
- Memory impact is minor but real: entries accumulate at one per unique `from` peer ID until the 5-minute GC tick.

---

### Likelihood Explanation

The attack requires only a standard P2P connection and two messages (`ConnectionRequest` then `ConnectionSync`). No special privileges, no PoW, no key material. The `from` field in both messages is fully attacker-controlled and unverified against the session's actual peer identity. The window is up to 5 minutes per entry.

---

### Recommendation

1. In `disconnected()`, remove all `pending_delivered` entries whose key matches the disconnecting peer's `PeerId`:
   ```rust
   async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
       if let Some(peer_id) = extract_peer_id(&context.session.address) {
           self.pending_delivered.remove(&peer_id);
       }
       self.rate_limiter.retain_recent();
       self.forward_rate_limiter.retain_recent();
   }
   ```
2. Verify that `content.from` in `ConnectionSync` matches the actual session's peer identity before consulting `pending_delivered`, preventing any peer from spoofing the `from` field to trigger NAT traversal on behalf of a disconnected peer.

---

### Proof of Concept

```
1. Attacker connects to victim (victim is the `to` target).
2. Attacker sends ConnectionRequest{from=A, to=victim, listen_addrs=[attacker_ip:port]}.
   → victim inserts pending_delivered[A] = ([attacker_ip:port], now).
3. Attacker disconnects.
   → disconnected() does NOT remove pending_delivered[A].
4. Assert: pending_delivered still contains key A.
5. Any peer (or attacker reconnected) sends ConnectionSync{from=A, to=victim, route=[]}.
   → victim finds pending_delivered[A], spawns try_nat_traversal to attacker_ip:port.
6. Attacker listens on attacker_ip:port, accepts the TCP connection.
   → raw_session() registers a new inbound session on the victim to attacker-controlled infrastructure.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-174)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-124)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
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
