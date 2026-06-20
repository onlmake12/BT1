The code is confirmed. Let me trace every claim precisely.

All claims are verifiable against the production code. Here is the full analysis.

---

### Title
`ANNOUNCE_INTERVAL` Bypassed on Every New Session via `last_announce: None` Initialization — (`network/src/protocols/discovery/state.rs`)

### Summary
`SessionState::new()` unconditionally sets `last_announce: None`. `check_timer()` treats `None` as "interval elapsed" via `.unwrap_or(true)`, so the very first `notify()` tick after any new session is established always qualifies the peer's address for announcement — regardless of how recently the peer connected. Because `SessionState` is destroyed on disconnect and recreated on reconnect, an attacker who cycles connections resets this timer on every reconnect, repeatedly injecting their address into the announcement pipeline and bypassing the intended 24-hour `ANNOUNCE_INTERVAL` gate.

### Finding Description

**Root cause — `last_announce: None` in constructor:** [1](#0-0) 

`last_announce` is always `None` at construction time.

**Root cause — `unwrap_or(true)` in `check_timer`:** [2](#0-1) 

When `last_announce` is `None`, `.map(...).unwrap_or(true)` returns `true` unconditionally, so the interval check is skipped entirely on the first call. `last_announce` is then set to `Some(now)`, so subsequent calls within `ANNOUNCE_INTERVAL` correctly return `None`. But a disconnect/reconnect resets the state to `None` again.

**The intended gate that is bypassed:** [3](#0-2) 

`ANNOUNCE_INTERVAL = 86400s`. The design intent is that a peer's address is re-announced at most once per 24 hours. This gate is fully bypassed on every new session.

**Propagation in `notify()`:** [4](#0-3) 

On each 60-second tick, `check_timer` is called for every session. If it returns `Some(addr)`, the address is pushed to `announce_list` and then queued for delivery to up to 3 randomly selected peer sessions.

**Preconditions the attacker must satisfy:**

1. `remote_addr` must be `RemoteAddress::Listen(...)` for `check_timer` to return `Some`. For inbound sessions it starts as `RemoteAddress::Init`; the attacker must send a `GetNodes` message with a `listen_port` field to trigger `update_port()` / `change_to_listen()`. [5](#0-4) 

2. `is_valid_addr` must pass (reachable public IP). [6](#0-5) 

3. `node_flags` must return `Some` (identify protocol must have completed and set `identify_info`). [7](#0-6) 

All three are achievable by any attacker running a real CKB node with a public IP.

**Partial mitigation — `addr_known` bloom filter:** [8](#0-7) 

Once a receiving peer's session has seen the attacker's address, `addr_known.contains` suppresses re-queuing within that session. This limits the "per-minute" rate claim: the same 3 peers will not receive the same address twice within their session lifetime. However, (a) the 3 targets are chosen randomly so different cycles may hit different peers, (b) `StableBloomFilter` probabilistically forgets entries over time, and (c) the attacker can connect to many different victim nodes simultaneously, each of which fans out to 3 of their own peers.

### Impact Explanation
An attacker with a public IP and a valid CKB node identity can inject their address into the peer stores of an unbounded number of nodes by cycling connections. Each new connection resets `last_announce` to `None`, causing the first `notify()` tick (≤60 s) to propagate the address to 3 peers of the victim. The `addr_known` filter limits repeated propagation to the same peer session, but does not prevent propagation to new peers or after session turnover. At scale this enables persistent address-space pollution and is a prerequisite for eclipse-attack positioning.

### Likelihood Explanation
The path requires only: a public IP, a valid CKB node binary, and the ability to open TCP connections. No privileged access, no PoW, no key material. The cycle (connect → send GetNodes with listen_port → wait ≤60 s → disconnect → reconnect) is fully automatable.

### Recommendation
Initialize `last_announce` to `Some(Instant::now())` in `SessionState::new()` so that a newly connected peer must wait a full `ANNOUNCE_INTERVAL` before its address becomes eligible for announcement. Alternatively, initialize it to `Some(Instant::now() - ANNOUNCE_INTERVAL + MINIMUM_OBSERVATION_PERIOD)` to enforce a minimum stability window before first announcement.

### Proof of Concept
```rust
// Pseudocode unit test
let mut state = SessionState {
    last_announce: None,
    remote_addr: RemoteAddress::Listen(some_multiaddr.clone()),
    ..
};
// First call — always Some regardless of elapsed time
assert!(state.check_timer(Instant::now(), ANNOUNCE_INTERVAL).is_some());
// Second call within interval — correctly None
assert!(state.check_timer(Instant::now(), ANNOUNCE_INTERVAL).is_none());
// Simulate reconnect: new SessionState resets last_announce to None
let mut state2 = SessionState { last_announce: None, remote_addr: RemoteAddress::Listen(some_multiaddr), .. };
// First call on new session — Some again, ANNOUNCE_INTERVAL fully bypassed
assert!(state2.check_timer(Instant::now(), ANNOUNCE_INTERVAL).is_some());
```

### Citations

**File:** network/src/protocols/discovery/state.rs (L84-91)
```rust
        SessionState {
            last_announce: None,
            addr_known,
            remote_addr,
            announce_multiaddrs: Vec::new(),
            received_get_nodes: false,
            received_nodes: false,
        }
```

**File:** network/src/protocols/discovery/state.rs (L94-109)
```rust
    pub(crate) fn check_timer(&mut self, now: Instant, interval: Duration) -> Option<&Multiaddr> {
        if self
            .last_announce
            .map(|time| now.saturating_duration_since(time) > interval)
            .unwrap_or(true)
        {
            self.last_announce = Some(now);
            if let RemoteAddress::Listen(addr) = &self.remote_addr {
                Some(addr)
            } else {
                None
            }
        } else {
            None
        }
    }
```

**File:** network/src/protocols/discovery/mod.rs (L36-36)
```rust
const ANNOUNCE_INTERVAL: Duration = Duration::from_secs(3600 * 24);
```

**File:** network/src/protocols/discovery/mod.rs (L124-139)
```rust
                            if let Some(port) = listen_port {
                                state.remote_addr.update_port(port);
                                state.addr_known.insert(state.remote_addr.to_inner());
                                // add client listen address to manager
                                if let RemoteAddress::Listen(ref addr) = state.remote_addr {
                                    let flags = self.addr_mgr.node_flags(session.id);
                                    self.addr_mgr.add_new_addr(
                                        session.id,
                                        (addr.clone(), flags.unwrap_or(Flags::COMPATIBILITY)),
                                    );
                                }
                            }
                            if version >= state::REUSE_PORT_VERSION {
                                // after enable reuse port, it can be broadcast
                                state.remote_addr.change_to_listen();
                            }
```

**File:** network/src/protocols/discovery/mod.rs (L224-262)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let now = Instant::now();
        // get announce list
        let mut announce_list = Vec::new();
        for (id, state) in self.sessions.iter_mut() {
            state.send_messages(context, *id).await;

            if let Some(addr) = state
                .check_timer(now, ANNOUNCE_INTERVAL)
                .filter(|addr| self.addr_mgr.is_valid_addr(addr))
                && let Some(flags) = self.addr_mgr.node_flags(*id)
            {
                announce_list.push((addr.clone(), flags));
            }
        }

        if !announce_list.is_empty() {
            let mut rng = rand::thread_rng();
            let mut keys = self.sessions.keys().cloned().collect::<Vec<_>>();
            for announce_multiaddr in announce_list {
                keys.shuffle(&mut rng);
                for key in keys.iter().take(3) {
                    if let Some(value) = self.sessions.get_mut(key) {
                        trace!(
                            ">> send {:?} to: {:?}, containing: {}",
                            announce_multiaddr,
                            value.remote_addr,
                            value.addr_known.contains(&announce_multiaddr)
                        );
                        if value.announce_multiaddrs.len() < ANNOUNCE_THRESHOLD
                            && !value.addr_known.contains(&announce_multiaddr)
                        {
                            value.announce_multiaddrs.push(announce_multiaddr.clone());
                            value.addr_known.insert(&announce_multiaddr);
                        }
                    }
                }
            }
        }
```

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
```

**File:** network/src/protocols/discovery/mod.rs (L397-402)
```rust
    fn node_flags(&self, id: SessionId) -> Option<Flags> {
        self.network_state.with_peer_registry(|reg| {
            reg.get_peer(id)
                .and_then(|peer| peer.identify_info.as_ref().map(|a| a.flags))
        })
    }
```
