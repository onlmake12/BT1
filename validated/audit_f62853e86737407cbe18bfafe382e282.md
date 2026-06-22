### Title
Peer Store Permanently Blocked by Group::None Flooding via Onion3 Discovery Addresses — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

The `check_purge` second-pass eviction logic uses `take(len / 2)` where `len` is the number of distinct network groups. When all 16384 stored addresses map to `Group::None` (as Onion3 addresses do), integer division yields `take(0)`, evicting nothing. Combined with the first-pass being a no-op (fresh entries are always connectable), `check_purge` returns `Err(PeerStoreError::EvictionFailed)` permanently, blocking all future `add_addr` calls.

---

### Finding Description

**Step 1 — Constants confirmed:**

`ADDR_COUNT_LIMIT = 16384` and `ADDR_MAX_RETRIES = 3`. [1](#0-0) 

**Step 2 — Onion3 → `Group::None`:**

`Group` is derived from `multiaddr_to_socketaddr`. Onion3 addresses return `None` from that function, so they fall through to `Group::None`. [2](#0-1) 

**Step 3 — First pass is a no-op for fresh entries:**

`is_connectable` returns `true` when `attempts_count < ADDR_MAX_RETRIES` (0 < 3), regardless of `last_connected_at_ms`. Discovery-received addresses are stored with `attempts_count = 0` via `AddrInfo::new`. [3](#0-2) [4](#0-3) 

**Step 4 — The broken `take(len / 2)` in the second pass:**

```rust
let len = peers_by_network_group.len();  // = 1 (only Group::None)
// ...
peers
    .into_iter()
    .take(len / 2)   // take(1/2) = take(0) — integer division!
    .flat_map(move |addrs| {
        if addrs.len() > 4 { Some(...evict 2...) } else { None }
    })
    .flatten()
    .collect()
```

With a single `Group::None` bucket containing all 16384 entries, `len = 1`, so `take(0)` iterates over nothing. `candidate_peers` is empty. [5](#0-4) 

**Step 5 — Permanent `EvictionFailed`:**

```rust
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
```

Every subsequent `add_addr` call hits `check_purge`, which immediately fails because the store is still at 16384 entries and the same logic repeats. [6](#0-5) 

---

### Impact Explanation

Once the store is saturated with `Group::None` entries, no legitimate IPv4/IPv6 peer address can ever be added. The node loses the ability to discover or reconnect to honest peers via the discovery protocol. Existing connections are unaffected, but peer rotation, reconnection after disconnect, and bootstrapping are all broken. This matches the **Medium (2001–10000)** scope: suboptimal state causing degraded network connectivity without direct consensus impact.

---

### Likelihood Explanation

An attacker needs only a single peer connection (or control of a discovery relay) to send `GetNodes` responses containing 16384 unique Onion3 `Multiaddr` values. Onion3 addresses have a 35-byte host field, providing a vast space of unique values. There is no rate limiting or per-source quota visible in `add_addr`. The attack is cheap, deterministic, and requires no special privileges beyond establishing one P2P connection.

---

### Recommendation

Fix the `take(len / 2)` integer division edge case. When `len = 1`, the expression evaluates to `0`, skipping all groups. Options:

1. Replace `take(len / 2)` with `take(len.saturating_add(1) / 2)` (ceiling division) so a single group is still considered.
2. Alternatively, use `take(std::cmp::max(1, len / 2))`.
3. Additionally, consider not treating `Group::None` as a single shared bucket — assign each non-IP address its own unique group key (e.g., hash of the full multiaddr) to prevent collapsing all Onion3 addresses into one eviction-resistant bucket.

---

### Proof of Concept

```rust
#[test]
fn test_group_none_eviction_failure() {
    use crate::peer_store::{PeerStore, ADDR_COUNT_LIMIT};
    use crate::Flags;
    use p2p::multiaddr::Multiaddr;

    let mut store = PeerStore::default();

    // Fill store with 16384 unique Onion3 addresses (Group::None)
    for i in 0u64..ADDR_COUNT_LIMIT as u64 {
        // Construct a unique Onion3 multiaddr per iteration
        let mut host = [0u8; 35];
        host[..8].copy_from_slice(&i.to_le_bytes());
        let onion_addr: Multiaddr = format!(
            "/onion3/{}:1234",
            base32::encode(base32::Alphabet::RFC4648 { padding: false }, &host)
        ).parse().unwrap();
        let _ = store.add_addr(onion_addr, Flags::COMPATIBILITY);
    }

    // Now try to add a legitimate IPv4 peer
    let ipv4_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
    let result = store.add_addr(ipv4_addr, Flags::COMPATIBILITY);

    // This will be Err(EvictionFailed) — the bug
    assert!(result.is_ok(), "Legitimate peer should be addable after eviction");
}
```

### Citations

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
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
