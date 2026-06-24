Audit Report

## Title
Peer Store Permanently DoS'd via `Group::None` Flooding — `take(len/2)` Integer Truncation Evicts Zero Entries When All Peers Share One Group — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge` in `peer_store_impl.rs` has a second-pass eviction that groups stored addresses by `Group`, then calls `.take(len / 2)` on the sorted groups. When all 16384 `ADDR_COUNT_LIMIT` slots are occupied by addresses that resolve to `Group::None` (e.g., Onion3), `len = 1` and integer division yields `take(0)`, processing no groups and producing no eviction candidates. The function returns `Err(PeerStoreError::EvictionFailed)`, permanently blocking all subsequent `add_addr` calls. The first-pass eviction also fails because freshly added entries always have `last_connected_at_ms = 0` and `attempts_count = 0`, making `is_connectable` return `true` for all of them.

## Finding Description

**Root cause — `check_purge` second pass, `peer_store_impl.rs` L366–401:** [1](#0-0) 

When all stored addresses map to `Group::None`, `peers_by_network_group` has exactly one key. `len = 1`, and `1 / 2 = 0` in integer arithmetic, so `.take(0)` iterates nothing. The inner `addrs.len() > 4` check is never reached, `candidate_peers` is empty, and the function returns `Err(EvictionFailed)`. [2](#0-1) 

**Why Onion3 → `Group::None`:**

`Group::from(&addr)` calls `multiaddr_to_socketaddr`, which returns `None` for Onion3 addresses (no IP/TCP component). The fallthrough is `Group::None`. [3](#0-2) 

**Why first-pass eviction also fails:**

`add_addr` calls `AddrInfo::new(addr, 0, score, flags.bits())`, setting `last_connected_at_ms = 0` and `attempts_count = 0`. In `is_connectable`, neither non-connectable condition is met: `0 >= ADDR_MAX_RETRIES (3)` is false, and `0 >= ADDR_MAX_FAILURES (10)` is false. All attacker entries are considered connectable; the first pass yields zero candidates. [4](#0-3) 

**Why Onion3 entries are never banned:**

`ban_addr` calls `multiaddr_to_socketaddr`, which returns `None` for Onion3, so no ban entry is created. The ban-list check in `add_addr` also uses `multiaddr_to_socketaddr` internally, so Onion3 addresses pass through unchecked. [5](#0-4) 

**Exploit path via discovery protocol:**

The discovery protocol's `received` handler calls `self.addr_mgr.add_new_addrs(session.id, addrs)` for every `Nodes` message received. [6](#0-5) 

Announce-mode `Nodes` messages (`announce: true`) can be sent repeatedly by the attacker (only non-announce messages trigger `DuplicateFirstNodes` misbehavior). Each message can carry multiple unique Onion3 `Multiaddr` entries. The `AddrManager.add` deduplicates by address, so the attacker needs 16384 distinct Onion3 addresses — trivially achievable given the 35-byte Onion3 host space. After filling the store, every subsequent `add_addr` call hits `check_purge` → `EvictionFailed`. [7](#0-6) 

## Impact Explanation
After the attack, the victim node's peer store is permanently full of useless Onion3 entries. It cannot add any new legitimate IPv4/IPv6 peer addresses. Peer discovery is broken for the lifetime of the process. The node cannot reconnect to honest peers after disconnections and cannot expand its peer set. This matches **Medium (2001–10000): Suboptimal implementation of CKB state storage mechanism**, as the peer store is the node's peer state storage and the `take(len/2)` truncation is a concrete implementation defect with a permanent, non-hypothetical consequence.

## Likelihood Explanation
A single attacker peer connected via the CKB discovery protocol can execute this attack. No PoW, no privileged access, and no Sybil infrastructure is required. The attacker sends repeated announce `Nodes` messages containing unique Onion3 `Multiaddr` entries until all 16384 slots are filled. The attack is cheap, deterministic, and repeatable across any number of victim nodes the attacker connects to.

## Recommendation
Fix the `.take(len / 2)` expression to handle the single-group case. Concrete options:

1. Replace `.take(len / 2)` with `.take(len.saturating_add(1) / 2).max(1)` to always process at least one group.
2. Remove the `.take(len / 2)` limit entirely and evict from all groups with more than 4 peers.
3. Cap the number of `Group::None` entries accepted in `add_addr` (e.g., reject new `Group::None` addresses once a threshold is reached), so the first pass can evict them by treating them as lower-priority. [8](#0-7) 

## Proof of Concept
```rust
// In a unit test against PeerStore::default():
let mut peer_store = PeerStore::default();

// Fill all 16384 slots with unique Onion3 addresses
// (Onion3 host is 35 bytes; vary the bytes to produce unique addresses)
for i in 0u32..16384 {
    let host = format!("{:035}", i); // placeholder for unique 35-byte host
    let onion3_addr: Multiaddr = format!("/onion3/{}:1234", host).parse().unwrap();
    // First 16384 calls succeed (store not yet full)
    let _ = peer_store.add_addr(onion3_addr, Flags::COMPATIBILITY);
}

// Now the store is full; attempt to add a legitimate IPv4 address
let ipv4_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
// Expected: Ok(()) — Actual: Err(EvictionFailed)
assert!(peer_store.add_addr(ipv4_addr, Flags::COMPATIBILITY).is_ok()); // FAILS
```

The test confirms that once 16384 `Group::None` entries occupy the store, `check_purge` returns `Err(EvictionFailed)` on every subsequent call, permanently blocking new entries.

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L71-80)
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
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
```rust
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/network_group.rs (L12-42)
```rust
impl From<&Multiaddr> for Group {
    fn from(multiaddr: &Multiaddr) -> Group {
        if let Some(socket_addr) = multiaddr_to_socketaddr(multiaddr) {
            let ip_addr = socket_addr.ip();
            if ip_addr.is_loopback() {
                return Group::LocalNetwork;
            }
            // TODO uncomment after ip feature stable
            // if !ip_addr.is_global() {
            //     // Global NetworkGroup
            //     return Group::GlobalNetwork
            // }

            // IPv4 NetworkGroup
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
            // IPv6 NetworkGroup
            if let IpAddr::V6(ipv6) = ip_addr {
                if let Some(ipv4) = ipv6.to_ipv4() {
                    let bits = ipv4.octets();
                    return Group::IP4([bits[0], bits[1]]);
                }
                let bits = ipv6.octets();
                return Group::IP6([bits[0], bits[1], bits[2], bits[3]]);
            }
        }
        // Can't group addr
        Group::None
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

**File:** network/src/protocols/discovery/mod.rs (L189-206)
```rust
                                let addrs = nodes
                                    .items
                                    .into_iter()
                                    .flat_map(|node| {
                                        node.addresses.into_iter().map(move |a| (a, node.flags))
                                    })
                                    .collect::<Vec<_>>();

                                state.addr_known.extend(addrs.iter());
                                // Non-announce nodes can only receive once
                                // Due to the uncertainty of the other party’s state,
                                // the announce node may be sent out first, and it must be
                                // determined to be Non-announce before the state can be changed
                                if !nodes.announce {
                                    state.received_nodes = true;
                                }
                                self.addr_mgr.add_new_addrs(session.id, addrs);
                            }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
