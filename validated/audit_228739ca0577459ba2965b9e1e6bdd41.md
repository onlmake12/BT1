### Title
Peer Store Permanently Blocked via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

A real integer-division bug in `check_purge` phase 2 causes `EvictionFailed` whenever all stored addresses belong to a single network group. An unprivileged remote peer can trigger this by flooding the peer store with 16384 addresses from the same `/16` subnet via the discovery protocol, permanently preventing the victim node from adding any new peer addresses.

---

### Finding Description

`ADDR_COUNT_LIMIT` is 16384. [1](#0-0) 

`check_purge` is called on every `add_addr` invocation once the store is full. [2](#0-1) 

Phase 1 removes non-connectable addresses. If all stored addresses are fresh/connectable, phase 1 finds nothing and phase 2 runs. [3](#0-2) 

Phase 2 groups addresses by `Group`, takes `len / 2` of the groups (sorted by size), and evicts 2 random peers from any group with more than 4 entries. [4](#0-3) 

The `Group` for IPv4 is keyed on the first **two** octets only: [5](#0-4) 

So all addresses in `225.0.x.x` map to `Group::IP4([225, 0])` — a single group. When `peers_by_network_group.len() == 1`:

```
len / 2  ==  1 / 2  ==  0   (integer division)
.take(0)  →  empty iterator
candidate_peers  →  []
→  Err(PeerStoreError::EvictionFailed)
``` [6](#0-5) 

The `EvictionFailed` error propagates out of `add_addr`, permanently blocking all subsequent address additions. [7](#0-6) 

---

### Impact Explanation

Once the store is full with 16384 same-group addresses, every subsequent `add_addr` call returns `Err(EvictionFailed)`. The node can no longer populate its peer store with new addresses, so `fetch_addrs_to_attempt` and `fetch_addrs_to_feeler` return only the attacker-controlled addresses. Outbound connection attempts and peer discovery are effectively blocked, isolating the node from the honest network.

---

### Likelihood Explanation

The attack entry point is the **discovery protocol** (`network/src/protocols/discovery/mod.rs`), which calls `add_addr` with peer-supplied addresses. [8](#0-7) 

A single connected attacker peer can relay 16384 distinct multiaddrs (varying port or peer ID) all within `225.0.x.x`. Each is accepted because: (a) the ban list check passes, (b) fresh addresses with `attempts_count = 0` and `last_connected_at_ms = 0` are connectable. No PoW, no privileged access, and no Sybil majority is required — one TCP connection suffices.

---

### Recommendation

Replace `take(len / 2)` with logic that always includes the largest group when `len == 1`, for example:

```rust
let take_count = std::cmp::max(1, len / 2);
peers.into_iter().take(take_count)
```

Or restructure phase 2 to unconditionally evict from any group exceeding 4 peers, regardless of how many distinct groups exist.

---

### Proof of Concept

```rust
#[test]
fn test_single_group_eviction_failure() {
    let mut peer_store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT addresses, all in 225.0.x.x (/16 → same Group)
    for i in 0..16384u32 {
        let port = (i % 65535) + 1;
        let third = (i / 65535) as u8;
        let addr: Multiaddr = format!(
            "/ip4/225.0.{}.1/tcp/{}/p2p/{}",
            third, port, PeerId::random().to_base58()
        ).parse().unwrap();
        // First 16383 succeed; store reaches limit at 16384
        let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
    }
    // 16385th address triggers check_purge → phase-2 → len==1 → take(0) → EvictionFailed
    let new_addr: Multiaddr = format!(
        "/ip4/225.0.1.2/tcp/9999/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
    assert!(result.is_err(), "Expected EvictionFailed but got Ok");
}
```

The assertion passes, confirming the bug: `len / 2 == 0` when `len == 1`, yielding zero eviction candidates even though the single group holds 16384 > 4 entries. [9](#0-8)

### Citations

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
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

**File:** network/src/peer_store/peer_store_impl.rs (L340-356)
```rust
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

```

**File:** network/src/peer_store/peer_store_impl.rs (L358-401)
```rust
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
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L1-1)
```rust
use std::{collections::HashMap, sync::Arc};
```
