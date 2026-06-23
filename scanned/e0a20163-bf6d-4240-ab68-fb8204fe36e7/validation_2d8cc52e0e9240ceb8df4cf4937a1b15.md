The code is clear enough to analyze. Let me trace through the exact logic.

## Analysis

### `check_purge` eviction logic [1](#0-0) 

When `addr_manager.count() >= ADDR_COUNT_LIMIT` and all addresses are connectable (step 1 removes nothing), the code enters the network-group eviction path: [2](#0-1) 

The critical lines are:

```rust
let len = peers_by_network_group.len();   // number of distinct /16 groups
peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));
peers
    .into_iter()
    .take(len / 2)          // <-- integer division
    .flat_map(move |addrs| {
        if addrs.len() > 4 { // <-- must have > 4 peers in group
            Some(...)
        } else {
            None
        }
    })
```

**When `len = 1` (all addresses from one /16):** `len / 2 = 0` via integer division → `take(0)` → zero groups examined → `candidate_peers` is empty → `Err(PeerStoreError::EvictionFailed)` is returned. [3](#0-2) 

### Error handling in the discovery path

The error is silently swallowed: [4](#0-3) 

So the node doesn't crash, but **every subsequent `add_addr` call also fails** as long as the store remains full with single-/16 addresses.

### Reachability

- `ADDR_COUNT_LIMIT = 16384`
- [5](#0-4) 
- A single `DiscoveryMessage::Nodes` can carry up to `MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` addresses = 3000 addresses per message. ~6 messages fill the store.
- [6](#0-5) 
- Freshly added addresses have `last_connected_at_ms = 0`, `attempts_count = 0`, which passes `is_connectable` (not yet exhausted `ADDR_MAX_RETRIES = 3`).
- [7](#0-6) 
- The `is_valid_addr` filter only requires `is_reachable(ip)` — a real public /16 block (e.g., `1.2.0.0/16`) has 65536 addresses, more than enough to fill 16384 slots with distinct IPs.
- [8](#0-7) 

### Verdict

---

### Title
Integer Division by 2 in `check_purge` Causes Permanent `EvictionFailed` When All Stored Addresses Share One /16 Network Group — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
When `addr_manager` is at capacity (`ADDR_COUNT_LIMIT = 16384`) and all stored addresses are connectable and belong to a single IPv4 /16 network group, `check_purge` computes `len / 2 = 1 / 2 = 0` (integer division), calls `take(0)`, finds no eviction candidates, and returns `Err(PeerStoreError::EvictionFailed)`. Every subsequent `add_addr` call fails silently, permanently blocking new address admission.

### Finding Description
In `PeerStore::check_purge` (`peer_store_impl.rs` lines 366–393), when the first eviction pass (removing non-connectable addresses) yields nothing, the code groups remaining addresses by /16 network group and attempts to evict from the top `len / 2` groups (where `len` is the number of distinct groups). When `len = 1`, integer division produces `0`, so `take(0)` examines no groups, `candidate_peers` remains empty, and the function returns `Err(PeerStoreError::EvictionFailed)` at line 400.

The caller `add_addr` propagates this error, but `DiscoveryAddressManager::add_new_addrs` (discovery/mod.rs line 355) catches it with a `debug!` log and discards it. The store remains full; no new address can ever be admitted until addresses age out naturally (7 days via `ADDR_TIMEOUT_MS`) or connection attempts exhaust `ADDR_MAX_RETRIES`/`ADDR_MAX_FAILURES`.

### Impact Explanation
The peer store can no longer learn new peer addresses. Outbound peer selection degrades to the stale set of attacker-supplied addresses from a single /16. The node's ability to discover honest peers is permanently impaired until the poisoned entries expire (up to 7 days). This does not affect consensus or funds but meaningfully degrades network-layer peer diversity and eclipse-resistance.

### Likelihood Explanation
An unprivileged peer reachable via the discovery protocol can send ~6 `DiscoveryMessage::Nodes` messages (each carrying up to 3000 addresses) using real public IPs from a single /16 block (65536 available). No special privileges, PoW, or Sybil majority are required. The attack is cheap and repeatable.

### Recommendation
Replace `take(len / 2)` with `take(len.saturating_add(1) / 2)` or `take((len + 1) / 2)` (ceiling division) so that even a single group is considered for eviction. Additionally, enforce a per-/16 cap at insertion time in `add_addr` to prevent any single network group from monopolizing the store.

### Proof of Concept
```rust
// Fill addr_manager with ADDR_COUNT_LIMIT connectable addresses, all from 1.2.x.x
let mut peer_store = PeerStore::default();
for i in 0u16..16384 {
    let ip = format!("1.2.{}.{}", i / 256, i % 256);
    let addr: Multiaddr = format!("/ip4/{}/tcp/8115", ip).parse().unwrap();
    // First call succeeds (store not yet full)
    let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
}
// Store is now at ADDR_COUNT_LIMIT, all addresses connectable, all in group IP4([1,2])
let new_addr: Multiaddr = "/ip4/1.2.64.1/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(Error::PeerStore(PeerStoreError::EvictionFailed))));
```

### Citations

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

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
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
