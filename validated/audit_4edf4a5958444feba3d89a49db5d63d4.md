Audit Report

## Title
Peer Store Permanently Deadlocked via Crafted Discovery Addresses — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge` in `peer_store_impl.rs` contains two compounding logic flaws in its Step 2 eviction path: `.take(len / 2)` examines only half of all network groups, and `addrs.len() > 4` excludes groups of exactly 4 peers. An attacker who fills the peer store with 4096 groups of exactly 4 connectable addresses (totaling `ADDR_COUNT_LIMIT = 16384`) causes every subsequent `add_addr` call to permanently return `Err(EvictionFailed)`, halting peer discovery on the victim node.

## Finding Description
**Constants confirmed:**
- `ADDR_COUNT_LIMIT = 16384` at `network/src/peer_store/mod.rs` L26 [1](#0-0) 
- `ADDR_MAX_RETRIES = 3` at `network/src/peer_store/mod.rs` L34 [2](#0-1) 

**`add_addr` inserts with `last_connected_at_ms=0` and `attempts_count=0`:** `AddrInfo::new` at `types.rs` L65–76 initializes `attempts_count` to `0`. [3](#0-2)  `add_addr` at `peer_store_impl.rs` L78 passes `0` as `last_connected_at_ms`. [4](#0-3) 

**`is_connectable` at `types.rs` L89–105:** Returns `false` only when `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES` (L95). With `attempts_count=0`, the condition `0 >= 3` is false, so all attacker-supplied addresses remain connectable. Step 1 of `check_purge` (L341–355) therefore finds no eviction candidates. [5](#0-4) 

**Step 2 eviction path at `peer_store_impl.rs` L358–401:**
1. Groups all addresses by network segment (`Group::IP4([bits[0], bits[1]])` — confirmed in `network_group.rs` L28). [6](#0-5) 
2. Sorts groups by descending size.
3. `.take(len / 2)` at L376 — examines only the top half of groups. [7](#0-6) 
4. `if addrs.len() > 4` at L378 — strictly greater than 4; groups of exactly 4 return `None`. [8](#0-7) 

With 4096 groups × 4 peers: `take(4096/2) = take(2048)` processes 2048 groups, but every group has `len == 4`, so `4 > 4` is false for all of them. `candidate_peers` remains empty and `Err(PeerStoreError::EvictionFailed)` is returned at L400 on every future `add_addr` call. [9](#0-8) 

`AddrManager::add` at `addr_manager.rs` L22–34 deduplicates by address, so the attacker must supply 16384 distinct addresses. The 16384th address is inserted successfully (count was 16383 < 16384 when `check_purge` ran), and every subsequent call fails. [10](#0-9) 

## Impact Explanation
Once the store is deadlocked, the victim node cannot accept any new peer addresses from the discovery protocol. As existing peers go offline, the node's connectivity degrades and it risks isolation from the honest chain tip. This constitutes a targeted peer discovery starvation attack reachable by any unprivileged peer, mapping to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — a single P2P connection can permanently degrade a node's network participation at zero ongoing cost.

## Likelihood Explanation
The attacker needs only one P2P connection to advertise 16384 addresses. The discovery protocol is designed to relay peer addresses, and `add_addr` imposes no per-source quota. The exact parameters (4096 groups × 4 peers) are trivially constructable: use addresses of the form `{a}.{b}.0.{1..=4}` for all 4096 combinations of `(a, b)` to produce 4096 distinct `/16` network groups. The attack is deterministic, requires no timing, no PoW, and no privileged access. It is permanently effective after a single burst of address advertisements.

## Recommendation
Fix both flaws in the Step 2 eviction path of `check_purge` in `network/src/peer_store/peer_store_impl.rs`:
1. Change `.take(len / 2)` to `.take(len)` (or remove the `take` entirely) so all network groups are considered for eviction.
2. Change `addrs.len() > 4` to `addrs.len() >= 2` (or at minimum `>= 4`) so groups at the boundary are also eligible for eviction.

Additionally, enforce a per-source limit on how many addresses a single discovery peer can contribute to the store to prevent a single connection from filling the entire address table.

## Proof of Concept
```rust
let mut peer_store = PeerStore::default();
// Fill with 4096 groups × 4 connectable peers = 16384 = ADDR_COUNT_LIMIT
for subnet in 0u16..4096 {
    let a = (subnet >> 8) as u8;
    let b = (subnet & 0xff) as u8;
    for host in 1u8..=4 {
        let addr: Multiaddr = format!(
            "/ip4/{}.{}.0.{}/tcp/8115/p2p/{}",
            a, b, host, PeerId::random().to_base58()
        ).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with 4096 groups of 4 connectable peers.
// Step 1: all peers connectable (attempts_count=0 < ADDR_MAX_RETRIES=3) → no candidates.
// Step 2: take(4096/2)=take(2048), all groups len==4, 4 > 4 is false → no candidates.
// Every subsequent add_addr permanently fails:
let new_addr: Multiaddr = format!(
    "/ip4/10.0.0.1/tcp/8115/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
assert!(matches!(
    peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
    Err(e) if e.to_string().contains("EvictionFailed")
));
```

### Citations

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L34-34)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
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

**File:** network/src/peer_store/peer_store_impl.rs (L374-390)
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
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
