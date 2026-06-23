### Title
Peer Store `check_purge` Eviction Failure Allows Unprivileged Peer to DoS Peer Discovery — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

The CKB peer store enforces a hard limit of `ADDR_COUNT_LIMIT = 16384` addresses. When this limit is reached, `check_purge` must successfully evict at least one entry before a new address can be added. The eviction logic has two strategies: (1) remove non-connectable addresses, and (2) remove addresses from network groups with more than 4 peers. An unprivileged peer can deliberately fill the store with 16384 addresses that are all connectable by default and spread across more than 4096 distinct `/16` network groups (≤4 per group), causing both eviction strategies to fail and returning `PeerStoreError::EvictionFailed`. After this, no new peer addresses can be added via the discovery protocol, permanently degrading the node's peer discovery capability for as long as the attacker's addresses remain connectable.

---

### Finding Description

**Root cause — `check_purge` in `network/src/peer_store/peer_store_impl.rs`:**

`add_addr` calls `check_purge` before inserting any new address. [1](#0-0) 

`check_purge` only runs when the store is at or above `ADDR_COUNT_LIMIT = 16384`. [2](#0-1) 

The two eviction strategies are:
1. Evict all addresses where `is_connectable()` returns `false`.
2. If strategy 1 found nothing, evict 2 random addresses from each network group that has **more than 4** peers, but only from the top half of groups by size.

If both strategies yield an empty candidate list, the function returns `Err(PeerStoreError::EvictionFailed)`. [3](#0-2) 

**Why freshly announced addresses are always connectable:**

`add_addr` creates every new `AddrInfo` with `last_connected_at_ms = 0` and `attempts_count = 0`. [4](#0-3) 

`is_connectable` returns `false` only when `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES (3)`, or when the address has been unreachable for more than 7 days with ≥10 failures. A freshly added address satisfies neither condition, so it is always connectable. [5](#0-4) 

**Attack path via the discovery protocol:**

The `DiscoveryAddressManager::add_new_addrs` function is called whenever a `Nodes` message is received from any connected peer. It calls `peer_store.add_addr` for each address and silently ignores `EvictionFailed` errors with only a debug log. [6](#0-5) 

A single non-announce `Nodes` message may carry up to `MAX_ADDR_TO_SEND = 1000` items, each with up to `MAX_ADDRS = 3` addresses, for a maximum of 3000 addresses per message. [7](#0-6) 

An attacker needs only ≈6 connections (6 × 3000 = 18000 > 16384) to fill the store. By using addresses from more than 4096 distinct `/16` subnets (≤4 addresses per subnet), the attacker ensures:
- Strategy 1 fails: all addresses are connectable (never tried, `attempts_count = 0`).
- Strategy 2 fails: no group has more than 4 peers.

After the store is full, `check_purge` returns `EvictionFailed` for every subsequent `add_addr` call, and no new peer addresses can be stored.

---

### Impact Explanation

The node's peer discovery is permanently degraded until the attacker's addresses naturally become non-connectable (after 3 failed feeler connection attempts each, `ADDR_MAX_RETRIES = 3`). [5](#0-4)  The feeler mechanism dials only a small number of addresses per interval, so exhausting 16384 fake addresses takes a very long time. During this window, the node cannot learn about any new peers via the discovery protocol. This is especially harmful for nodes bootstrapping from scratch or recovering from connection loss, as they rely on peer discovery to find sync partners. The error is silently swallowed, so operators receive no alert. [8](#0-7) 

---

### Likelihood Explanation

Any peer that can establish a TCP connection to the victim node can execute this attack. No authentication, no special privilege, and no on-chain funds are required. The attacker only needs to:
1. Connect ~6 times (within the node's `max_inbound` limit).
2. Send one `Nodes` message per connection containing 1000 items × 3 addresses from diverse public `/16` subnets.

The addresses do not need to be controlled by the attacker — any routable public IPs suffice. The attack is cheap, repeatable, and the error is invisible to operators.

---

### Recommendation

1. **Add a last-resort eviction strategy**: when both primary strategies yield no candidates, evict the `N` addresses with the oldest `last_tried_at_ms` (or `last_connected_at_ms == 0` with the highest `attempts_count`), regardless of group membership.
2. **Rate-limit address ingestion per peer session**: cap the number of new addresses accepted from a single session to a small fraction of `ADDR_COUNT_LIMIT`.
3. **Prefer evicting never-connected addresses first**: addresses with `last_connected_at_ms == 0` and `attempts_count == 0` have the lowest trust and should be the first candidates for eviction when the store is full.

---

### Proof of Concept

```
1. Attacker opens 6 TCP connections to the victim CKB node.
2. On each connection, after the discovery handshake, the attacker sends one
   DiscoveryMessage::Nodes { announce: false, items: [1000 Node entries] }
   where each Node has 3 addresses from distinct /16 subnets
   (e.g., 1.0.0.1, 2.0.0.1, 3.0.0.1, ..., 18000.0.0.1 mapped to real /16 ranges).
3. After 6 messages, the peer store contains ≥16384 entries, all with
   last_connected_at_ms=0, attempts_count=0 (connectable), spread across
   >4096 /16 groups (≤4 per group).
4. Any subsequent call to peer_store.add_addr() triggers check_purge(),
   which finds no non-connectable peers and no group with >4 members,
   and returns Err(PeerStoreError::EvictionFailed).
5. add_new_addrs() logs a debug message and discards the error.
6. The victim node can no longer learn about any new peers via discovery.
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

**File:** network/src/protocols/discovery/mod.rs (L30-34)
```rust
const ANNOUNCE_THRESHOLD: usize = 10;
// The maximum number of new addresses to accumulate before announcing.
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L347-363)
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
    }
```
