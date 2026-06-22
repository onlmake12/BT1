The network grouping implementation is critical to this analysis. The `Group` type uses the **first two octets** of IPv4 addresses (`IP4([bits[0], bits[1]])`), making it a /16-equivalent grouping — not /24 as the question claims. Despite this factual error in the question's framing, the core vulnerability mechanism is real and reachable.

---

### Title
`check_purge` Second-Pass Eviction Blind Spot Allows Peer Store Lockout via Crafted Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
The `check_purge` function's second-pass eviction uses a strict `> 4` guard. An attacker who fills the peer store with exactly 4 connectable addresses per network group across 4096 groups (totalling `ADDR_COUNT_LIMIT = 16384`) causes every subsequent `add_addr` call to return `Err(EvictionFailed)`, permanently blocking new peer discovery for the lifetime of the node.

### Finding Description

`check_purge` is called by `add_addr` before inserting any new address. It has two eviction passes:

**Pass 1** — evict non-connectable peers: [1](#0-0) 

A freshly-added `AddrInfo` has `last_connected_at_ms=0` and `attempts_count=0`. With these defaults, `is_connectable` returns `true` because `attempts_count < ADDR_MAX_RETRIES (3)`: [2](#0-1) 

So if all 16384 entries are freshly advertised (never tried), Pass 1 finds zero candidates.

**Pass 2** — group-based eviction: [3](#0-2) 

The grouping key is `Group::IP4([bits[0], bits[1]])` — the first **two** octets of the IPv4 address (a /16-equivalent group, not /24 as the question states): [4](#0-3) 

With 4096 groups of exactly 4 peers each:
- `len = 4096`, `take(len / 2)` = `take(2048)` — iterates 2048 groups
- The guard `if addrs.len() > 4` is **false** for every group (4 is not strictly greater than 4)
- Every group returns `None`, so `candidate_peers` is empty
- Line 399–400 returns `Err(PeerStoreError::EvictionFailed)`

**Error handling in the Discovery path silently swallows this error:** [5](#0-4) 

The loop continues but every subsequent address also fails. The Identify protocol logs an error but also continues: [6](#0-5) 

DNS seeding also silently discards the error: [7](#0-6) 

### Impact Explanation

Once the store is locked:
- No new peer addresses can be added via Discovery, Identify, or DNS seeding
- `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` still operate on existing (attacker-controlled) entries
- The node cannot expand its peer set or recover from peer churn
- The state persists until the node attempts connections to the fake addresses and accumulates ≥3 failures per address (slow, and the attacker can re-fill)

### Likelihood Explanation

The attacker needs to advertise 16384 globally routable IP addresses spread across 4096 distinct /16 networks (first two octets). The attacker does **not** need to control those IPs — the Discovery protocol accepts addresses without verifying reachability at ingestion time. `is_valid_addr` only checks `is_reachable` (globally routable), which any valid public IP satisfies. A single malicious connected peer can send multiple `Nodes` messages over time to fill the store. There is no per-session cap on total addresses contributed.

### Recommendation

Change the strict inequality `> 4` to `>= 4` (or `> 3`) on line 378:

```rust
// Before
if addrs.len() > 4 {

// After
if addrs.len() >= 4 {
```

This ensures groups of exactly 4 are also eligible for eviction in the second pass, closing the blind spot. Additionally, consider adding a per-session rate limit on the total number of addresses a single peer may contribute to the store.

### Proof of Concept

Pre-fill `addr_manager` with 16384 `AddrInfo` entries: 4 per /16 group across 4096 groups (e.g., `1.0.x.y` through `16.0.x.y`), all with `last_connected_at_ms=0` and `attempts_count=0` (so `is_connectable=true`). Then call `add_addr` with a fresh address and assert `Err(EvictionFailed)` is returned. The existing `test_eviction` test in `network/src/tests/peer_store.rs` confirms the second-pass path works when groups exceed 4, but no test covers the exactly-4 boundary case. [8](#0-7) [9](#0-8)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L327-404)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }

        // Evicting invalid data in the peer store is a relatively rare operation
        // There are certain cleanup strategies here:
        // 1. First evict the nodes that have reached the eviction condition
        // 2. If the first step is unsuccessful, enter the network segment grouping mode
        //  2.1. Group current data according to network segment
        //  2.2. Sort according to the amount of data in the same network segment
        //  2.3. In the network segment with more than 4 peer, randomly evict 2 peer

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let candidate_peers: Vec<_> = self
            .addr_manager
            .addrs_iter()
            .filter_map(|addr| {
                if !addr.is_connectable(now_ms) {
                    Some(addr.addr.clone())
                } else {
                    None
                }
            })
            .collect();

        for key in candidate_peers.iter() {
            self.addr_manager.remove(key);
        }

        if candidate_peers.is_empty() {
            let candidate_peers: Vec<_> = {
                let mut peers_by_network_group: HashMap<Group, Vec<_>> = HashMap::default();
                for addr in self.addr_manager.addrs_iter() {
                    peers_by_network_group
                        .entry((&addr.addr).into())
                        .or_default()
                        .push(addr);
                }
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
                    .flat_map(move |addrs| {
                        if addrs.len() > 4 {
                            Some(
                                addrs
                                    .iter()
                                    .choose_multiple(&mut rand::thread_rng(), 2)
                                    .into_iter()
                                    .map(|addr| addr.addr.clone())
                                    .collect::<Vec<Multiaddr>>(),
                            )
                        } else {
                            None
                        }
                    })
                    .flatten()
                    .collect()
            };

            for key in candidate_peers.iter() {
                self.addr_manager.remove(key);
            }

            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
        }
        Ok(())
    }
```

**File:** network/src/peer_store/types.rs (L89-105)
```rust
    pub fn is_connectable(&self, now_ms: u64) -> bool {
        // do not remove addr tried in last minute
        if self.tried_in_last_minute(now_ms) {
            return true;
        }
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
        // consider addr is not connectable if failed too many times
        if now_ms.saturating_sub(self.last_connected_at_ms) > ADDR_TIMEOUT_MS
            && (self.attempts_count >= ADDR_MAX_FAILURES)
        {
            return false;
        }
        true
    }
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L354-361)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
```

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
```

**File:** network/src/services/dns_seeding/mod.rs (L110-114)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                let _ = peer_store.add_addr(addr, Flags::empty());
            }
        });
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
