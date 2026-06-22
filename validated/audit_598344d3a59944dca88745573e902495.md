The `check_purge` function is actually in `network/src/peer_store/peer_store_impl.rs`, not `network_group.rs` as the question states. However, the mathematical flaw described is real and confirmed in the code.

---

### Title
Peer Store Permanently Blocked via Crafted Group-of-4 Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

The `check_purge` function contains a logical dead zone: the combination of `take(len / 2)` (integer division) and the `addrs.len() > 4` strict threshold means that when all network groups contain exactly 4 peers, zero eviction candidates are ever produced, causing `EvictionFailed` even when the store is full of connectable peers.

### Finding Description

In `check_purge`, when no non-connectable peers exist, the group-based eviction path runs: [1](#0-0) 

```
let len = peers_by_network_group.len();   // e.g. 3
...
peers.into_iter()
    .take(len / 2)                        // take(1) when len=3
    .flat_map(move |addrs| {
        if addrs.len() > 4 {              // 4 > 4 is false
            Some(...)
        } else {
            None
        }
    })
```

With `len = 3`, `take(3 / 2)` = `take(1)`. If the single considered group has exactly 4 peers, `4 > 4` is `false`, `flat_map` returns `None`, and `candidate_peers` is empty. The function then hits: [2](#0-1) 

This generalizes: any odd number of groups where all groups have exactly 4 peers triggers the same dead zone. With `N` groups of 4 peers (N odd), `take(N/2)` considers `(N-1)/2` groups, all with exactly 4 peers, none passing `> 4`.

### Impact Explanation

`add_addr` calls `check_purge` before inserting: [3](#0-2) 

When `check_purge` returns `Err(EvictionFailed)`, `add_addr` propagates the error. The peer store permanently rejects new address insertions. The node cannot discover new peers, and if existing peers disconnect, the node becomes isolated.

### Likelihood Explanation

An attacker participating in the P2P discovery protocol can advertise crafted addresses. By distributing exactly 4 addresses per `/16` IPv4 block across enough blocks to reach `ADDR_COUNT_LIMIT`, and ensuring all are connectable (fresh `last_connected_at_ms`, low `attempts_count`), the attacker fills the store into the dead zone. The connectable check at: [4](#0-3) 

...means freshly-injected addresses with `last_connected_at_ms = 0` and `attempts_count < ADDR_MAX_RETRIES` pass as connectable, bypassing the first eviction pass entirely.

### Recommendation

Replace the strict `> 4` with `>= 4` (or `> 3`), and replace `take(len / 2)` with `take((len + 1) / 2)` (ceiling division) to ensure at least one group is always considered when the store is full. Additionally, add a fallback that evicts from the largest group unconditionally if no candidates are found after the group pass.

### Proof of Concept

```
// Fill store with ceil(ADDR_COUNT_LIMIT / 4) groups, each with exactly 4 peers
// from distinct /16 blocks (e.g., 1.1.x.x, 2.2.x.x, 3.3.x.x, ...)
// All peers: last_connected_at_ms=0, attempts_count=0 (connectable)
// Result: check_purge returns EvictionFailed, add_addr returns Err
// New legitimate peers cannot be added to the store
```

The exact flaw: `take(len / 2)` with integer division silently drops the remainder group when `len` is odd, and `> 4` (not `>= 4`) creates a threshold that groups of exactly 4 can never cross. [5](#0-4)

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
