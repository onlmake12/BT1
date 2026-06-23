Looking at the exact code path in detail before rendering a verdict.

### Title
Unbounded Per-Connection `get_random(2500)` Peer-Store Scan via Reconnect Loop — (`network/src/protocols/discovery/mod.rs`)

### Summary

The `received_get_nodes` duplicate-guard is scoped to a single TCP session. An attacker who cycles TCP connections (connect → send `GetNodes` → disconnect → repeat) triggers one full `fetch_random(2500)` peer-store scan per connection with no rate limiting, causing sustained CPU load and peer-store mutex contention on the victim node.

---

### Finding Description

The guard in the `GetNodes` handler checks `state.received_get_nodes` before calling `get_random`: [1](#0-0) 

`received_get_nodes` is initialized to `false` in every new `SessionState`: [2](#0-1) 

So the guard only blocks a *second* `GetNodes` on the *same* session. A fresh TCP connection always starts with `received_get_nodes = false`, meaning the first `GetNodes` on every new connection unconditionally reaches `get_random(2500)`.

`get_random` acquires the `peer_store` mutex and calls `fetch_random_addrs(2500, flags)`: [3](#0-2) 

`fetch_random_addrs` delegates to `addr_manager.fetch_random`, which iterates over **all** entries in `random_ids` performing an in-place Fisher-Yates shuffle, HashMap lookups, and IP deduplication — holding the mutex for the entire traversal: [4](#0-3) 

The loop only exits early when 2500 results are collected; if the store is large or the filter is selective, it walks the entire store.

The inbound connection limit (`max_inbound = max_peers − max_outbound_peers = 125 − 8 = 117` by default) does not prevent the reconnect pattern — when the limit is reached the node *evicts* an existing peer rather than rejecting the new connection: [5](#0-4) 

There is no IP-based reconnect rate limit or ban triggered by rapid reconnects alone.

---

### Impact Explanation

Each new inbound connection forces one O(N) peer-store scan (N = store size, up to `ADDR_COUNT_LIMIT`) while holding the `peer_store` mutex. Sustained reconnect loops from one or more attackers:

- Increase CPU usage on the victim node proportional to reconnect rate × store size.
- Serialize all other `peer_store` operations (outbound dialing, address bookkeeping) behind the mutex, introducing latency into peer management.

The impact is **local to the victim node**, not "the whole network." "CPU/memory exhaustion" is overstated for a bounded O(N) operation, but measurable degradation of peer-store responsiveness is realistic under a sustained reconnect flood.

---

### Likelihood Explanation

The attack requires only the ability to open TCP connections to the victim's P2P port (default 8115) — no authentication, no PoW, no special protocol knowledge beyond sending a valid `GetNodes` message. The reconnect loop is trivially scriptable. The only natural throttle is TCP handshake latency and the eviction overhead, neither of which provides meaningful protection against a determined attacker with reasonable bandwidth.

---

### Recommendation

1. **Add a per-IP or per-session-origin rate limit** on how frequently `GetNodes` responses are served, independent of the session-level `received_get_nodes` flag.
2. **Move the `get_random` call after all early-exit checks**, or add a global cooldown (e.g., serve at most one `get_random` response per IP per N seconds).
3. **Consider banning IPs** that repeatedly open connections and immediately send `GetNodes` without completing normal protocol handshakes.

---

### Proof of Concept

```
for i in 1..N:
    tcp_connect(victim:8115)
    send(DiscoveryMessage::GetNodes { count: 1000, ... })
    # victim executes get_random(2500) → fetch_random_addrs → full store scan
    tcp_disconnect()
```

Measure `peer_store` mutex hold time and CPU usage as N increases. Assert that total peer-store scans per second scales linearly with reconnect rate and is unbounded by any guard in the production code path. [1](#0-0) [6](#0-5)

### Citations

**File:** network/src/protocols/discovery/mod.rs (L109-120)
```rust
                        if let Some(state) = self.sessions.get_mut(&session.id) {
                            if state.received_get_nodes && check(Misbehavior::DuplicateGetNodes) {
                                if context.disconnect(session.id).await.is_err() {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                                return;
                            }

                            state.received_get_nodes = true;
                            // must get the item first, otherwise it is possible to load
                            // the address of peer listen.
                            let mut items = self.addr_mgr.get_random(2500, required_flags);
```

**File:** network/src/protocols/discovery/mod.rs (L375-391)
```rust
    fn get_random(&mut self, n: usize, flags: Flags) -> Vec<(Multiaddr, Flags)> {
        let fetch_random_addrs = self
            .network_state
            .with_peer_store_mut(|peer_store| peer_store.fetch_random_addrs(n, flags));
        let addrs = fetch_random_addrs
            .into_iter()
            .filter_map(|paddr| {
                if !self.is_valid_addr(&paddr.addr) {
                    return None;
                }
                let f = Flags::from_bits_truncate(paddr.flags);
                Some((paddr.addr, f))
            })
            .collect();
        trace!("Discovered random addrs: {:?}", addrs);
        addrs
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
