The vulnerability is real and concrete. Here is the full analysis:

---

### Title
Peer Store Permanent DoS via Crafted Discovery Addresses Exploiting Off-by-One in `check_purge` Eviction Guard — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`check_purge` contains a strict `> 4` threshold for network-group eviction. An attacker who fills the peer store with exactly 4 addresses per `/16` group across enough distinct groups can make every subsequent `add_addr` call return `PeerStoreError::EvictionFailed`, permanently blocking honest peer address admission.

### Finding Description

`check_purge` runs in two stages when `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384):

**Stage 1** — evict non-connectable peers: [1](#0-0) 

A freshly added address has `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` only returns `false` when `attempts_count >= ADDR_MAX_RETRIES (3)` for never-connected peers, so brand-new addresses are always connectable. [2](#0-1) 

**Stage 2** — network-group eviction (only entered when Stage 1 finds nothing): [3](#0-2) 

The critical flaw is the strict inequality on line 378:

```rust
if addrs.len() > 4 {   // groups of exactly 4 are NEVER evicted
``` [4](#0-3) 

If every network group has exactly 4 entries, the `flat_map` produces nothing, `candidate_peers` is empty, and the function returns:

```rust
return Err(PeerStoreError::EvictionFailed.into());
``` [5](#0-4) 

**Attack construction:**
- `ADDR_COUNT_LIMIT = 16384`
- 4096 distinct `/16` IPv4 groups × 4 addresses each = 16384 total
- All addresses freshly added → all connectable → Stage 1 finds nothing
- All groups have `len == 4` → Stage 2 condition `> 4` never fires → `EvictionFailed` [6](#0-5) 

### Impact Explanation

Every call to `add_addr` after the store is saturated returns `Err(PeerStoreError::EvictionFailed)`. The node can no longer learn any new peer addresses from the discovery protocol, DNS seeding, or identify handshakes. This enables:

- **Network isolation**: the victim node is stuck with only the attacker-supplied addresses
- **Eclipse attack enablement**: the attacker controls which peers the victim can reach

The `add_addr` call site in the discovery protocol propagates this error: [7](#0-6) 

### Likelihood Explanation

The attacker needs 16384 routable IP addresses spread across 4096 distinct `/16` subnets (4 per subnet). This is achievable with a mid-sized botnet or cloud provider with diverse IP allocations. The discovery protocol accepts `Nodes` messages from any connected peer with no per-session address-count rate limit visible in the code. The attacker only needs to establish a single connection and send crafted `Nodes` responses. [8](#0-7) 

### Recommendation

Change the eviction threshold from strict `> 4` to `>= 4` (or `> 3`):

```rust
// Before (vulnerable):
if addrs.len() > 4 {

// After (fixed):
if addrs.len() >= 4 {
```

Additionally, consider:
1. Capping the number of addresses accepted per discovery session
2. Preferring eviction of addresses that have never been successfully connected to (`last_connected_at_ms == 0`) [9](#0-8) 

### Proof of Concept

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
            // First 16383 succeed
            let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
        }
    }
    // 16384th and beyond must fail
    let new_addr: Multiaddr = "/ip4/200.200.1.5/tcp/8115".parse().unwrap();
    assert!(matches!(
        peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
        Err(e) if e.to_string().contains("EvictionFailed")
    ));
}
``` [10](#0-9)

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

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L341-355)
```rust
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

**File:** network/src/peer_store/peer_store_impl.rs (L357-403)
```rust
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
