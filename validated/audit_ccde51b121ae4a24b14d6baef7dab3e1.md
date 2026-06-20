### Title
Peer Store Permanent DoS via Crafted Discovery Addresses Exhausting All Eviction Paths — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

A remote peer can permanently prevent a victim CKB node from adding any new peer addresses to its store by filling `PeerStore` to `ADDR_COUNT_LIMIT` (16 384) with fabricated addresses distributed across ≥4 096 distinct /16 subnets (≤4 per group). Once full under these conditions, `check_purge()` exhausts both eviction strategies and returns `Err(PeerStoreError::EvictionFailed)`. `DiscoveryAddressManager::add_new_addrs()` silently discards this error, permanently blocking peer discovery.

---

### Finding Description

**Step 1 — Attacker-controlled entry point**

The discovery protocol's `received` handler calls `self.addr_mgr.add_new_addrs(session.id, addrs)` when a `Nodes` message arrives. [1](#0-0) 

`DiscoveryAddressManager::add_new_addrs` iterates the supplied addresses and calls `peer_store.add_addr()` for each, catching any error only at `debug!` level:

```rust
if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
    debug!("Failed to add discovered address to peer_store {:?} {:?}", err, addr);
}
``` [2](#0-1) 

The error is swallowed; no retry, no fallback, no caller notification.

**Step 2 — `add_addr` propagates `check_purge` failure**

`add_addr` calls `self.check_purge()?` before inserting. If `check_purge` returns `Err`, the `?` propagates it back to `add_new_addrs`, which discards it. [3](#0-2) 

**Step 3 — `check_purge` has two eviction strategies, both defeatable**

`ADDR_COUNT_LIMIT` is 16 384. [4](#0-3) 

*Strategy 1* — remove non-connectable entries. `is_connectable()` returns `true` for any freshly added address because `attempts_count = 0 < ADDR_MAX_RETRIES (3)` and `attempts_count = 0 < ADDR_MAX_FAILURES (10)`: [5](#0-4) 

`AddrInfo::new` always initialises `attempts_count = 0` and `last_connected_at_ms = 0`: [6](#0-5) 

So every attacker-injected address passes `is_connectable`, and strategy 1 finds zero candidates.

*Strategy 2* — network-group eviction. The `Group` type uses the first two octets of IPv4 (a /16 subnet): [7](#0-6) 

Eviction only fires for groups with `addrs.len() > 4`: [8](#0-7) 

If the attacker fills the store with exactly 4 addresses per /16 subnet (4 096 subnets × 4 = 16 384), every group has size 4, the `> 4` condition is never true, `candidate_peers` remains empty, and the function returns:

```rust
return Err(PeerStoreError::EvictionFailed.into());
``` [9](#0-8) 

**Step 4 — Filling the store via P2P**

Per the protocol limits, a non-announce `Nodes` message may carry up to `MAX_ADDR_TO_SEND` (1 000) items × `MAX_ADDRS` (3) addresses = 3 000 addresses per session response. Announce messages add up to `ANNOUNCE_THRESHOLD` (10) items per message. Across ~6 connections the attacker injects 16 384 fabricated addresses from distinct /16 subnets. No PoW, no key, no privileged role is required — only a standard P2P connection. [10](#0-9) 

---

### Impact Explanation

Once the store is saturated under these conditions, every subsequent `add_addr` call triggers `check_purge`, which permanently returns `Err`. All new peer addresses received via discovery are silently dropped. The victim node cannot refresh its peer set, cannot learn about new nodes, and is effectively isolated from network topology updates. Existing connections are unaffected, but the node cannot replace lost peers or discover new ones.

---

### Likelihood Explanation

The attack requires only a handful of standard P2P connections and the ability to send `Nodes` messages containing fabricated (but syntactically valid) multiaddrs. IPv4 has 65 536 possible /16 subnets; selecting 4 096 of them is trivial. No real nodes need to exist at those addresses. The attacker does not need to maintain the connections after injection. The condition is persistent across restarts if the peer store is persisted to disk.

---

### Recommendation

1. **Enforce a per-/16-subnet cap at insertion time** in `add_addr` (e.g., reject if the group already has ≥4 entries), preventing the store from ever reaching the degenerate all-groups-≤4 state.
2. **Make `check_purge` always succeed** by falling back to evicting the oldest-tried or lowest-scored entries when both primary strategies yield no candidates, instead of returning `Err`.
3. **Treat `EvictionFailed` as a warning-level event** (not silent debug) so operators can detect the condition.

---

### Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill with 4 addresses per /16 subnet across 4096 distinct subnets
for subnet_a in 1u8..=16 {
    for subnet_b in 0u8..=255 {
        for port in 1u16..=4 {
            let addr: Multiaddr = format!(
                "/ip4/{}.{}.0.1/tcp/{}/p2p/{}",
                subnet_a, subnet_b, port,
                PeerId::random().to_base58()
            ).parse().unwrap();
            peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
        }
    }
}
// Store is now at ADDR_COUNT_LIMIT (16384), all connectable, all groups <= 4
assert_eq!(peer_store.addr_manager().addrs_iter().count(), 16384);

// Any new address is now permanently rejected
let new_addr: Multiaddr = format!(
    "/ip4/200.0.0.1/tcp/42/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // Err(EvictionFailed)
assert_eq!(peer_store.addr_manager().addrs_iter().count(), 16384); // unchanged
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L205-205)
```rust
                                self.addr_mgr.add_new_addrs(session.id, addrs);
```

**File:** network/src/protocols/discovery/mod.rs (L266-299)
```rust
fn verify_nodes_message(nodes: &Nodes) -> Option<Misbehavior> {
    let mut misbehavior = None;
    if nodes.announce {
        if nodes.items.len() > ANNOUNCE_THRESHOLD {
            warn!(
                "Number of nodes exceeds announce threshold {}",
                ANNOUNCE_THRESHOLD
            );
            misbehavior = Some(Misbehavior::TooManyItems {
                announce: nodes.announce,
                length: nodes.items.len(),
            });
        }
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }

    if misbehavior.is_none() {
        for item in &nodes.items {
            if item.addresses.len() > MAX_ADDRS {
                misbehavior = Some(Misbehavior::TooManyAddresses(item.addresses.len()));
                break;
            }
        }
    }

    misbehavior
```

**File:** network/src/protocols/discovery/mod.rs (L354-360)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
```

**File:** network/src/peer_store/peer_store_impl.rs (L378-390)
```rust
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/types.rs (L65-76)
```rust
    pub fn new(addr: Multiaddr, last_connected_at_ms: u64, score: Score, flags: u64) -> Self {
        AddrInfo {
            // only store tcp protocol
            addr: base_addr(&addr),
            score,
            last_connected_at_ms,
            last_tried_at_ms: 0,
            attempts_count: 0,
            random_id_pos: 0,
            flags,
        }
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
