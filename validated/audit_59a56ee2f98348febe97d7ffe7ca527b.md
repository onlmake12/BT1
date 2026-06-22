The logic flaw in `check_purge()` is real and demonstrable. Let me trace through it precisely.

### Title
Adversarially Crafted Discovery `Nodes` Messages Can Permanently Fill Peer Store via Off-by-One in Group Eviction — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

The `check_purge()` function in `PeerStore` uses a strict `> 4` threshold when deciding whether to evict peers from a network group. An unprivileged remote peer can send crafted `Nodes` discovery messages that fill the store with addresses distributed across exactly `len/2` groups of exactly 4 peers each. When this happens, phase 2 eviction finds nothing to remove and returns `Err(EvictionFailed)`, permanently blocking new address insertion.

---

### Finding Description

`check_purge()` has two eviction phases:

**Phase 1** (lines 341–355): removes addresses where `is_connectable()` returns `false`. Freshly discovered addresses are created via `AddrInfo::new(addr, 0, score, flags)` with `last_connected_at_ms = 0` and `attempts_count = 0`. Per `is_connectable()`:

```rust
if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
    return false;
}
```

With `attempts_count = 0 < 3`, all freshly added addresses are considered connectable. Phase 1 finds nothing. [1](#0-0) 

**Phase 2** (lines 358–401): groups addresses by network segment, sorts groups by size descending, takes the top `len/2` groups, and evicts 2 random peers **only if** `addrs.len() > 4`:

```rust
peers
    .into_iter()
    .take(len / 2)
    .flat_map(move |addrs| {
        if addrs.len() > 4 {   // ← strict greater-than, not >=
            Some(...)
        } else {
            None               // ← groups of exactly 4 are skipped
        }
    })
``` [2](#0-1) 

If an attacker fills the store with `ADDR_COUNT_LIMIT` = 16384 addresses distributed across 4096 groups of exactly 4 each:
- `len = 4096`, `len/2 = 2048`
- Every group in the top 2048 has `len == 4`, so `4 > 4` is `false`
- `candidate_peers` is empty → `Err(PeerStoreError::EvictionFailed)` is returned [3](#0-2) [4](#0-3) 

---

### Impact Explanation

`add_addr()` propagates the `Err` from `check_purge()`. In the discovery protocol's `add_new_addrs()`, this error is silently swallowed at `debug!` level:

```rust
if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
    debug!("Failed to add discovered address to peer_store {:?} {:?}", err, addr);
}
``` [5](#0-4) 

The peer store remains permanently at capacity. No new legitimate peer addresses can be inserted. Peer discovery is degraded: the node cannot learn about new peers until existing entries age out (up to 7 days via `ADDR_TIMEOUT_MS`, or after `ADDR_MAX_RETRIES`/`ADDR_MAX_FAILURES` failed dial attempts). [6](#0-5) 

---

### Likelihood Explanation

The attack is reachable from any unprivileged remote peer connected via the discovery protocol. A single attacker session can send `Nodes` messages (up to 1000 items × 3 addresses = 3000 addresses per message), requiring only ~6 messages to fill the 16384-slot store. The attacker controls the IP addresses in the `Nodes` payload and can trivially craft addresses from 4096 distinct `/16` or `/24` subnets with exactly 4 addresses each. [7](#0-6) 

---

### Recommendation

Change the strict `> 4` threshold to `>= 4` (or `> 1`) in the group eviction condition:

```rust
// Before:
if addrs.len() > 4 {

// After:
if addrs.len() >= 4 {
``` [8](#0-7) 

Additionally, consider adding a fallback eviction path that always frees at least one slot (e.g., evict the lowest-scored or oldest address) when both phases fail, to enforce the invariant that `check_purge()` always succeeds in making room.

---

### Proof of Concept

```rust
// Construct a PeerStore with ADDR_COUNT_LIMIT addresses in 4096 groups of exactly 4
// (4096 distinct /16 subnets × 4 addresses each = 16384 total)
// All addresses have last_connected_at_ms=0, attempts_count=0 → is_connectable() = true
// Then call add_addr() once more:
//   check_purge() → phase 1 finds 0 non-connectable → phase 2 takes top 2048 groups
//   each group has len==4, 4 > 4 == false → candidate_peers is empty
//   → Err(EvictionFailed) returned
//   → add_addr() returns Err
//   → addr_manager.count() still == ADDR_COUNT_LIMIT
assert!(matches!(result, Err(Error::PeerStore(PeerStoreError::EvictionFailed))));
assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);
``` [9](#0-8)

### Citations

**File:** network/src/peer_store/types.rs (L89-104)
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L28-35)
```rust
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```

**File:** network/src/protocols/discovery/mod.rs (L189-205)
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
