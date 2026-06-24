All code claims verified against the actual source. Here is the validated audit report:

---

Audit Report

## Title
`check_purge` integer division `take(len / 2)` yields `take(0)` when all addresses map to `Group::None`, permanently blocking peer store insertion — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
When the peer store reaches `ADDR_COUNT_LIMIT` (16384) and all stored addresses map to `Group::None` (e.g., Onion3 multiaddrs), `check_purge` computes `take(1 / 2)` = `take(0)` via Rust integer division, selecting zero eviction candidates and returning `Err(PeerStoreError::EvictionFailed)`. Because `add_addr` propagates this error via `?`, every subsequent peer address insertion fails permanently, preventing the node from learning new peers.

## Finding Description
**Root cause — `Group::None` grouping:**
`Group::from(&Multiaddr)` in `network/src/network_group.rs` calls `multiaddr_to_socketaddr`, which returns `None` for Onion3 addresses (no IP component), causing the function to fall through to `Group::None`. [1](#0-0) 

All 16384 Onion3 addresses therefore land in a single `HashMap` entry in `peers_by_network_group`.

**Step 1 (lines 341–355) — no eviction candidates:**
Freshly added addresses have `last_connected_at_ms = 0` and `attempts_count = 0` (set by `AddrInfo::new` at `types.rs` line 65–76, called from `add_addr` line 78 with `0` as the timestamp). [2](#0-1) 

`is_connectable` returns `true` for these: `tried_in_last_minute` is false (last_tried_at_ms=0), the `attempts_count >= ADDR_MAX_RETRIES` guard (3) is not met, and the `attempts_count >= ADDR_MAX_FAILURES` guard (10) is not met. [3](#0-2) 

Step 1 removes nothing.

**Step 2 (lines 357–401) — integer division to zero:**
With all addresses in one group, `len = 1`. Line 376 computes `take(len / 2)` = `take(1 / 2)` = `take(0)` in Rust integer arithmetic. The iterator yields no groups. [4](#0-3) 

The secondary guard `if addrs.len() > 4` at line 378 is never reached because `take(0)` short-circuits the iterator before entering `flat_map`. [5](#0-4) 

`candidate_peers` is empty → `return Err(PeerStoreError::EvictionFailed)` at line 400. [6](#0-5) 

**Propagation:**
`add_addr` calls `self.check_purge()?` at line 75, propagating the error to every caller. [7](#0-6) 

`ADDR_COUNT_LIMIT` is 16384. [8](#0-7) 

## Impact Explanation
Once the store is saturated with Onion3 addresses, the node cannot add any new peer addresses from the discovery protocol. On restart, the 16384 Onion3 addresses are reloaded from the database and the store remains blocked. The node cannot bootstrap new connections, effectively isolating it from the CKB P2P network. Applied at scale this degrades CKB network connectivity. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attacker requires only a single discovery protocol connection to the target node and the ability to send `AddressesMessage` packets containing distinct Onion3 multiaddrs. No proof-of-work, no privileged role, and no Sybil majority is required. The 35-byte Onion3 host space provides far more than 16384 distinct addresses. The attack is repeatable after every node restart.

## Recommendation
Replace `take(len / 2)` with `take((len + 1) / 2)` (ceiling division) at line 376 to guarantee at least one group is selected when `len >= 1`. [9](#0-8) 

Additionally, enforce a per-group sub-limit on `Group::None` addresses (e.g., cap at `ADDR_COUNT_LIMIT / 4`) so that ungroupable addresses cannot monopolize the store.

## Proof of Concept
```rust
#[test]
fn test_check_purge_onion3_dos() {
    let mut store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT (16384) distinct Onion3 addresses
    for i in 0u64..16383 {
        let mut host = [0u8; 35];
        host[..8].copy_from_slice(&i.to_le_bytes());
        let onion3 = Protocol::Onion3((host, 1234).into());
        let addr: Multiaddr = std::iter::once(onion3).collect();
        store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
    let mut host = [0xfeu8; 35];
    let onion3 = Protocol::Onion3((host, 1234).into());
    let addr: Multiaddr = std::iter::once(onion3).collect();
    store.add_addr(addr, Flags::COMPATIBILITY).unwrap();

    // Any further add_addr must now fail with EvictionFailed
    let mut host = [0xffu8; 35];
    let onion3 = Protocol::Onion3((host, 9999).into());
    let addr: Multiaddr = std::iter::once(onion3).collect();
    let result = store.add_addr(addr, Flags::COMPATIBILITY);
    assert!(result.is_err());
    assert!(result.unwrap_err().to_string().contains("EvictionFailed"));
}
```

### Citations

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

**File:** network/src/peer_store/types.rs (L63-76)
```rust
impl AddrInfo {
    /// Init
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

**File:** network/src/peer_store/peer_store_impl.rs (L377-390)
```rust
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
