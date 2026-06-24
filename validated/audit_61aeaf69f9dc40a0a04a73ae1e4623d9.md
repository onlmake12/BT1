Audit Report

## Title
Session-Scoped `received_get_nodes` Guard Allows Unbounded Per-Connection `fetch_random` Peer-Store Scans via Reconnect Loop — (`network/src/protocols/discovery/mod.rs`)

## Summary
The duplicate-`GetNodes` guard in the discovery protocol is scoped to a single TCP session. Because `received_get_nodes` is reset to `false` on every new `SessionState`, an attacker who cycles TCP connections (connect → send `GetNodes` → disconnect → repeat) triggers one full `fetch_random(2500)` peer-store scan per connection. There is no per-IP or global rate limit on this path, causing measurable `peer_store` mutex contention and continuous eviction of legitimate inbound peers.

## Finding Description
The guard at [1](#0-0)  checks `state.received_get_nodes` before calling `get_random`, but `received_get_nodes` is initialized to `false` in every new `SessionState` at [2](#0-1)  so the guard only blocks a *second* `GetNodes` on the *same* session. Every fresh TCP connection unconditionally reaches `get_random(2500)` at [3](#0-2) .

`get_random` calls `with_peer_store_mut` → `fetch_random_addrs` → `addr_manager.fetch_random`, which runs a Fisher-Yates in-place shuffle over `random_ids` with `HashMap` lookups and IP deduplication, holding the `peer_store` mutex for the entire traversal. [4](#0-3)  The store can hold up to `ADDR_COUNT_LIMIT = 16384` entries. [5](#0-4) 

The `fetch_random_addrs` call path is confirmed: `get_random` (mod.rs L375-391) → `peer_store.fetch_random_addrs` (peer_store_impl.rs L269-283) → `addr_manager.fetch_random` (addr_manager.rs L45-96). [6](#0-5) 

The inbound connection limit does not block the reconnect pattern: when `non_whitelist_inbound >= max_inbound`, the node *evicts* an existing peer rather than rejecting the new connection. [7](#0-6)  The only pre-connection check is `is_addr_banned`, which only blocks IPs that have already been explicitly banned — rapid reconnects alone do not trigger a ban. [8](#0-7) 

The discovery protocol has no rate limiter, unlike the hole-punching protocol which uses a `governor::RateLimiter` keyed by session and message type. [9](#0-8)  The `misbehave` handler always returns `MisbehaveResult::Disconnect` but this only fires on a *second* `GetNodes` within the same session, not across sessions. [10](#0-9) 

## Impact Explanation
Each new inbound connection forces one O(N) peer-store scan (N ≤ 16384) while holding the `peer_store` mutex. A sustained reconnect flood serializes all other `peer_store` operations (outbound dialing, address bookkeeping) behind the mutex, introducing latency into peer management. Additionally, the eviction-on-full-inbound behavior means the attacker continuously displaces legitimate inbound peers, degrading the victim node's connectivity. This matches **Low (501–2000 points): Any other important performance improvements for CKB**. A crash is not claimed — the operation is bounded — but measurable peer-store responsiveness degradation and continuous peer eviction are realistic under a sustained flood.

## Likelihood Explanation
The attack requires only the ability to open TCP connections to the victim's P2P port (default 8115) and send a valid `GetNodes` message — no authentication, no PoW, no special protocol knowledge beyond the public discovery message format. The reconnect loop is trivially scriptable. The only natural throttle is TCP handshake latency and eviction overhead, neither of which provides meaningful protection against a determined attacker with reasonable bandwidth.

## Recommendation
1. Add a per-IP cooldown on how frequently `get_random` responses are served (e.g., at most one response per source IP per N seconds), independent of the session-level `received_get_nodes` flag, mirroring the `governor::RateLimiter` pattern already used in the hole-punching protocol.
2. Consider tracking rapid-reconnect behavior per IP and applying a temporary ban after a threshold is exceeded, using the existing `ban_addr` / `ban_network` infrastructure.
3. Move the `get_random` call after all early-exit checks, or cache the last response and reuse it within a short window to reduce mutex hold time.

## Proof of Concept
```
for i in 1..N:
    tcp_connect(victim:8115)
    send(DiscoveryMessage::GetNodes { count: 1000, ... })
    # victim executes get_random(2500) → fetch_random_addrs → fetch_random (up to 16384 entries) under peer_store mutex
    tcp_disconnect()
```
Instrument `fetch_random` with timing metrics. Assert that total peer-store scans per second scales linearly with reconnect rate and is unbounded by any guard in the production code path. Measure `peer_store` mutex hold time and CPU usage as N increases; confirm no rate-limiting mechanism activates. The existing fuzz target at `network/fuzz/fuzz_targets/fuzz_peer_store.rs` can be extended to benchmark `fetch_random` under load. [11](#0-10)

### Citations

**File:** network/src/protocols/discovery/mod.rs (L109-115)
```rust
                        if let Some(state) = self.sessions.get_mut(&session.id) {
                            if state.received_get_nodes && check(Misbehavior::DuplicateGetNodes) {
                                if context.disconnect(session.id).await.is_err() {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                                return;
                            }
```

**File:** network/src/protocols/discovery/mod.rs (L120-120)
```rust
                            let mut items = self.addr_mgr.get_random(2500, required_flags);
```

**File:** network/src/protocols/discovery/mod.rs (L365-373)
```rust
    fn misbehave(&mut self, session: &SessionContext, behavior: &Misbehavior) -> MisbehaveResult {
        error!(
            "DiscoveryProtocol detects abnormal behavior, session: {:?}, behavior: {:?}",
            session, behavior
        );

        // FIXME:
        MisbehaveResult::Disconnect
    }
```

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

**File:** network/src/peer_store/addr_manager.rs (L45-96)
```rust
    pub fn fetch_random<F>(&mut self, count: usize, filter: F) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
        let mut duplicate_ips = HashSet::new();
        let mut addr_infos = Vec::with_capacity(count);
        let mut rng = rand::thread_rng();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        for i in 0..self.random_ids.len() {
            // reuse the for loop to shuffle random ids
            // https://en.wikipedia.org/wiki/Fisher%E2%80%93Yates_shuffle
            let j = rng.gen_range(i..self.random_ids.len());
            self.swap_random_id(j, i);
            let addr_info: AddrInfo = self.id_to_info[&self.random_ids[i]].to_owned();
            match multiaddr_to_socketaddr(&addr_info.addr) {
                Some(socket_addr) => {
                    let ip = socket_addr.ip();
                    let is_unique_ip = !duplicate_ips.contains(&ip);
                    // A trick to make our tests work
                    // TODO remove this after fix the network tests.
                    let is_test_ip = ip.is_unspecified() || ip.is_loopback();
                    if (is_test_ip || is_unique_ip)
                        && addr_info.is_connectable(now_ms)
                        && filter(&addr_info)
                    {
                        duplicate_ips.insert(ip);
                        addr_infos.push(addr_info);
                    }
                }
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
                        } else {
                            debug!(
                                "addr {:?} is not connectable and not an onion address",
                                addr_info.addr
                            );
                        }
                    }
                }
            }
            if addr_infos.len() == count {
                break;
            }
        }
        addr_infos
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/peer_store_impl.rs (L269-283)
```rust
    pub fn fetch_random_addrs(&mut self, count: usize, required_flags: Flags) -> Vec<AddrInfo> {
        // Get info:
        // 1. Connected within 7 days

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let addr_expired_ms = now_ms.saturating_sub(ADDR_TIMEOUT_MS);

        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };

        // get success connected addrs.
        self.addr_manager.fetch_random(count, filter)
    }
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** network/src/peer_registry.rs (L115-121)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```
