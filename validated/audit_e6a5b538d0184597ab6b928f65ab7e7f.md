Audit Report

## Title
Peer Store Phase-2 Eviction Deadlock via `addrs.len() > 4` Off-by-One and `take(len / 2)` Truncation — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

The `check_purge` function in `PeerStore` contains two compounding logic defects in its phase-2 eviction path. When an attacker fills the peer store to `ADDR_COUNT_LIMIT` (16,384) with addresses distributed across ≥ 4,096 distinct network groups of exactly 4 entries each, phase-2 produces zero eviction candidates and returns `PeerStoreError::EvictionFailed` on every subsequent `add_addr` call. Because this error is silently swallowed in the discovery and identify protocols, the node permanently loses the ability to learn new peer addresses via P2P, leading to progressive network isolation.

## Finding Description

**Phase-1 eviction (lines 341–355):** `check_purge` first collects addresses for which `is_connectable` returns `false`. Addresses inserted via `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0`. [1](#0-0) 

`is_connectable` only returns `false` for never-connected addresses when `attempts_count >= ADDR_MAX_RETRIES (3)`. Fresh attacker-supplied addresses have `attempts_count = 0`, so they are all connectable and phase-1 finds nothing to evict. [2](#0-1) 

**Phase-2 eviction (lines 358–401):** When phase-1 yields nothing, the code groups all addresses by network group (`Group::IP4([bits[0], bits[1]])` — the first two octets of the IPv4 address), sorts groups by descending size, then applies two guards: [3](#0-2) 

1. **`take(len / 2)` integer truncation** — with 4,096 groups, `take(4096 / 2)` selects exactly 2,048 groups. All selected groups have size 4 (the maximum, since all groups are equal).
2. **`addrs.len() > 4` strict inequality** — groups of exactly 4 entries evaluate `4 > 4 = false` and return `None`. Every selected group is skipped. [4](#0-3) 

`candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. [5](#0-4) 

**Attack entry point:** `DiscoveryAddressManager::add_new_addrs` calls `peer_store.add_addr` for every address in a received `Nodes` message and silently swallows `EvictionFailed` at debug log level. [6](#0-5) 

The same path exists in `IdentifyProtocol::add_remote_listen_addrs`, which logs at `error` level but still discards the result. [7](#0-6) 

**Attack construction:** The network group is keyed on the first two octets of IPv4 (`[bits[0], bits[1]]`), giving 65,025 possible distinct groups. An attacker sends `Nodes` messages advertising 4 addresses per `/16` group across 4,096 distinct groups (e.g., `1.0.x.x` through `16.255.x.x`), totalling exactly 16,384 entries. `MAX_ADDR_TO_SEND = 1000` per message means ~17 messages suffice. [8](#0-7) [9](#0-8) 

## Impact Explanation

Once the peer store is saturated in the adversarial configuration, every subsequent `add_addr` call returns `Err(EvictionFailed)`. The node silently drops all newly discovered peer addresses from both the discovery and identify protocols. As existing connections drop through natural churn, the node cannot find replacements and becomes progressively isolated from the network. Applied at scale across many nodes simultaneously, this disrupts the CKB network's peer discovery fabric with minimal attacker cost, matching the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." [10](#0-9) 

## Likelihood Explanation

- Any peer that can establish a P2P connection can send `Nodes` messages — no special privilege required.
- Total data volume: 16,384 addresses × ~50 bytes ≈ 800 KB across ~17 messages.
- Advertised IPs need not be reachable; the discovery protocol stores them without verification.
- The attacker must periodically re-advertise to prevent natural 7-day expiry (`ADDR_TIMEOUT_MS`), but this is trivial.
- Feeler connections will eventually mark some entries non-connectable (after 3 failed attempts), but the attacker can continuously refill those slots faster than the node exhausts them, since feeler connections are rate-limited. [11](#0-10) 

## Recommendation

Fix both defects in `check_purge` phase-2:

1. Change `addrs.len() > 4` to `addrs.len() >= 4` (or equivalently `> 3`) so groups of exactly 4 are eligible for eviction.
2. Change `take(len / 2)` to `take((len + 1) / 2)` (ceiling division) to avoid truncating odd-length group lists.
3. Add an unconditional fallback: if phase-2 still produces zero candidates, evict the oldest-tried entry rather than returning `EvictionFailed`. [12](#0-11) 

## Proof of Concept

```rust
#[test]
fn test_eviction_deadlock_4_per_group() {
    let mut peer_store = PeerStore::default();
    // Fill with 4096 groups × 4 addresses each = 16384 total
    // Each group is a distinct /16 (first two octets vary)
    let mut count = 0;
    'outer: for a in 1u8..=16 {
        for b in 0u8..=255 {
            for c in 1u8..=4 {
                if count >= 16384 { break 'outer; }
                let addr: Multiaddr = format!(
                    "/ip4/{}.{}.1.{}/tcp/8115/p2p/{}",
                    a, b, c, PeerId::random().to_base58()
                ).parse().unwrap();
                peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
                count += 1;
            }
        }
    }
    assert_eq!(peer_store.addr_manager().count(), 16384);

    // 16385th add_addr must fail with EvictionFailed
    let extra: Multiaddr = format!(
        "/ip4/200.0.0.1/tcp/8115/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    let result = peer_store.add_addr(extra, Flags::COMPATIBILITY);
    assert!(result.is_err(), "Expected EvictionFailed, got Ok");
}
```

The test fills the store with exactly 4,096 distinct `/16` groups of 4 addresses each, then asserts that the 16,385th insertion fails. Both defects (`take(len / 2)` selecting 2,048 groups all of size 4, and `addrs.len() > 4` rejecting all of them) are exercised on the exact boundary condition. [13](#0-12)

### Citations

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

**File:** network/src/peer_store/types.rs (L89-97)
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
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L32-32)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
```

**File:** network/src/protocols/discovery/mod.rs (L347-362)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
        }
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L27-28)
```rust
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
```

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
