Audit Report

## Title
Peer Store Permanent DoS via Off-by-One in `check_purge` Eviction Guard — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` uses a strict `> 4` threshold when selecting candidates for network-group eviction. An attacker who fills the peer store with exactly 4 addresses per `/16` group across 4096 distinct groups (totaling 16384 = `ADDR_COUNT_LIMIT`) causes every subsequent `add_addr` call to return `PeerStoreError::EvictionFailed`, permanently blocking honest peer address admission and enabling eclipse attacks.

## Finding Description

`check_purge` is called at the start of every `add_addr` invocation. [1](#0-0) 

When `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384), it runs two eviction stages. [2](#0-1) 

**Stage 1** collects addresses where `!is_connectable(now_ms)`. A freshly injected address has `last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`. Evaluating `is_connectable`: [3](#0-2) 
- `tried_in_last_minute`: `0 >= now_ms - 60000` → false
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)`: `0 >= 3` → false
- `now_ms.saturating_sub(0) > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES(10)`: second condition `0 >= 10` → false

All fresh addresses return `true` from `is_connectable`, so Stage 1 evicts nothing.

**Stage 2** groups addresses by network group. The `Group` for IPv4 is `IP4([bits[0], bits[1]])` — the first two octets, i.e., the `/16` subnet. [4](#0-3) 

The eviction loop takes the top `len/2` groups sorted by size descending, and for each group: [5](#0-4) 

With 4096 groups of exactly 4 addresses each, `addrs.len() > 4` evaluates to `4 > 4` → false for every group, so every group returns `None`. `candidate_peers` is empty, and the function returns: [6](#0-5) 

`AddrManager::add` deduplicates by exact address, so the attacker uses 4 distinct IPs per `/16` group to avoid deduplication. [7](#0-6) 

Critically, the `is_global()` routability check in `network_group.rs` is commented out, meaning private/non-routable IPs are accepted and grouped normally. [8](#0-7)  This means the attacker does not need 16384 routable IPs — any crafted IP addresses work, making injection trivially achievable via a single discovery session.

## Impact Explanation

Every `add_addr` call after the store is saturated returns `Err(EvictionFailed)`. The node permanently loses the ability to learn new peer addresses from discovery, DNS seeding, or identify handshakes. The node is left with only the 16384 attacker-supplied addresses, enabling a full eclipse attack. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** via isolation. If the eclipsed node is a block producer, it can be fed a fake chain, escalating to **Critical: Vulnerabilities which could easily cause consensus deviation**.

## Likelihood Explanation

Because the `is_global()` check is commented out, the attacker does not need routable IPs. Any 16384 crafted IP addresses across 4096 `/16` subnets suffice. The discovery protocol accepts `Nodes` messages from any connected peer with no visible per-session address-count rate limit. A single TCP connection is sufficient to inject all 16384 addresses. The attack is permanent until the node is restarted with a cleared peer store.

## Recommendation

Change the eviction threshold from strict `> 4` to `>= 4` at `peer_store_impl.rs` line 378:

```rust
// Before (vulnerable):
if addrs.len() > 4 {

// After (fixed):
if addrs.len() >= 4 {
```

Additionally:
1. Cap the number of addresses accepted per discovery session to limit injection speed.
2. Prefer eviction of addresses with `last_connected_at_ms == 0` (never successfully connected) before applying network-group eviction.
3. Uncomment and enable the `is_global()` check in `network_group.rs` to reject non-routable addresses.

## Proof of Concept

```rust
#[test]
fn test_eviction_failed_with_exactly_4_per_group() {
    let mut peer_store = PeerStore::default();
    // Fill 4096 /16 groups × 4 addresses each = 16384 total
    for group in 0u16..4096 {
        let hi = (group >> 8) as u8;
        let lo = (group & 0xff) as u8;
        for host in 1u8..=4 {
            let addr: Multiaddr = format!("/ip4/{}.{}.1.{}/tcp/8115", hi, lo, host)
                .parse().unwrap();
            let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
        }
    }
    // Any subsequent add_addr must fail with EvictionFailed
    let new_addr: Multiaddr = "/ip4/200.200.1.5/tcp/8115".parse().unwrap();
    assert!(matches!(
        peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
        Err(e) if e.to_string().contains("EvictionFailed")
    ));
}
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L71-75)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
```

**File:** network/src/peer_store/peer_store_impl.rs (L374-392)
```rust
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

**File:** network/src/network_group.rs (L19-23)
```rust
            // TODO uncomment after ip feature stable
            // if !ip_addr.is_global() {
            //     // Global NetworkGroup
            //     return Group::GlobalNetwork
            // }
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/peer_store/addr_manager.rs (L22-34)
```rust
    pub fn add(&mut self, mut addr_info: AddrInfo) {
        if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
            let (exist_last_connected_at_ms, random_id_pos) = {
                let info = self.id_to_info.get(&id).expect("must exists");
                (info.last_connected_at_ms, info.random_id_pos)
            };
            // Get time earlier than record time, return directly
            if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
                addr_info.random_id_pos = random_id_pos;
                self.id_to_info.insert(id, addr_info);
            }
            return;
        }
```
