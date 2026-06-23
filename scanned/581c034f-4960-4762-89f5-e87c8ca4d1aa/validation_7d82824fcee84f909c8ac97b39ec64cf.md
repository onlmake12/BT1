The code logic is concrete and traceable. Let me walk through each step.

---

## Vulnerability Analysis

### Step 1: Trace the eviction logic in `check_purge()`

When `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384), `check_purge()` runs two eviction passes:

**Pass 1** — remove non-connectable addresses: [1](#0-0) 

`is_connectable()` returns `true` when `last_connected_at_ms == 0 && attempts_count < ADDR_MAX_RETRIES (3)`. [2](#0-1) 

Newly discovered addresses via `add_addr()` are inserted with `last_connected_at_ms=0` and `attempts_count=0`, so they are all connectable. Pass 1 yields zero candidates.

**Pass 2** — network-group eviction (entered only when Pass 1 yields nothing): [3](#0-2) 

The critical condition is:
```rust
if addrs.len() > 4 {
    // evict 2 random peers from this group
} else {
    None  // skip this group entirely
}
``` [4](#0-3) 

### Step 2: The off-by-one flaw

The network group is keyed by `/16` IPv4 prefix (`Group::IP4([bits[0], bits[1]])`): [5](#0-4) 

If an attacker populates exactly **4 addresses per /16 subnet** across **4096 distinct /16 subnets** (4096 × 4 = 16384 = `ADDR_COUNT_LIMIT`):

- Pass 1: all addresses are connectable → zero candidates
- Pass 2: every group has `len == 4`, so `4 > 4` is **false** → `None` for every group → zero candidates
- Result: `return Err(PeerStoreError::EvictionFailed.into())` [6](#0-5) 

### Step 3: The error is silently swallowed

`add_addr()` propagates the error via `?`: [7](#0-6) 

`DiscoveryAddressManager::add_new_addrs()` catches it with only a `debug!` log — no ban, no disconnect, no alert: [8](#0-7) 

### Step 4: Attacker entry path

A single connected peer sends `Nodes` messages via the discovery protocol. Each message can carry up to `MAX_ADDR_TO_SEND=1000` items × `MAX_ADDRS=3` addresses = 3000 addresses per message: [9](#0-8) 

The addresses are fake but pass `is_valid_addr()` (which only checks `is_reachable()`, not actual connectivity). IPv4 has 65536 possible /16 subnets, so 4096 distinct ones are trivially available. ~6 Nodes messages suffice to fill the store.

---

### Title
Off-by-one in `check_purge()` eviction condition allows remote peer to permanently freeze peer discovery — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
The eviction condition `addrs.len() > 4` in `check_purge()` never fires when every network group contains exactly 4 addresses. A single malicious peer can exploit this by advertising 4096 × 4 fake addresses across distinct /16 subnets, filling `ADDR_COUNT_LIMIT=16384` and causing every subsequent `add_addr()` call to return `Err(EvictionFailed)`, silently blocking all future peer discovery until node restart.

### Finding Description
`check_purge()` has two eviction passes. Pass 1 removes non-connectable addresses; Pass 2 groups addresses by `/16` subnet and evicts 2 random peers from any group with `len > 4`. The off-by-one (`> 4` instead of `>= 4`) means groups of exactly 4 are never evicted. An attacker who fills the store with exactly 4 addresses per /16 subnet across 4096 subnets satisfies both conditions simultaneously: all addresses are connectable (Pass 1 yields nothing) and no group exceeds 4 (Pass 2 yields nothing). The result is a permanent `EvictionFailed` error on every subsequent `add_addr()` call.

### Impact Explanation
Peer discovery is permanently frozen. The node cannot learn new peer addresses from the network until restarted. The error is silently swallowed at the `debug!` log level, so operators have no indication. Existing connections are unaffected, but the node's ability to find new peers (e.g., after disconnections) is eliminated.

### Likelihood Explanation
Requires only one P2P connection. The attacker sends ~6 `Nodes` messages containing fake addresses from 4096 different /16 subnets. No PoW, no privileged access, no Sybil attack required. The addresses need only pass `is_reachable()`, not actually be connectable.

### Recommendation
Change the eviction condition from `addrs.len() > 4` to `addrs.len() >= 4` (or equivalently `> 3`) in `check_purge()`: [10](#0-9) 

Additionally, consider rate-limiting the number of addresses accepted per session and per source /16 subnet during ingestion in `add_new_addrs()`.

### Proof of Concept
```rust
// Fill store with 4096 groups × 4 addresses each (all connectable)
for subnet in 0u16..4096 {
    for host in 1u8..=4 {
        let ip = format!("{}.{}.1.{}", subnet >> 8, subnet & 0xff, host);
        let addr = format!("/ip4/{}/tcp/8115/p2p/Qm...", ip).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// 16385th add_addr must fail with EvictionFailed
let extra = "/ip4/200.0.1.1/tcp/8115/p2p/Qm...".parse().unwrap();
let result = peer_store.add_addr(extra, Flags::COMPATIBILITY);
assert!(matches!(result, Err(Error::PeerStore(PeerStoreError::EvictionFailed))));
assert_eq!(peer_store.addr_manager().count(), 16384); // store frozen
```

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

**File:** network/src/peer_store/peer_store_impl.rs (L357-402)
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

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
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
