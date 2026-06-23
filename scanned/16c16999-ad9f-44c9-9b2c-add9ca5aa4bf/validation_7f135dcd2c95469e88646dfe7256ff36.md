Now I have all the information needed to analyze this thoroughly. Let me trace every relevant code path.

### Title
Peer Store Sybil Flood via Discovery `Nodes` Messages Bypasses `check_purge` Eviction — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

An unprivileged remote peer can flood the peer store with up to `ADDR_COUNT_LIMIT` (16 384) sybil addresses by sending `Nodes` discovery messages containing attacker-controlled public IPs. Because `add_addr` hardcodes `last_connected_at_ms = 0` for all discovered addresses, and because `is_connectable` returns `true` for any address that has never been tried (`attempts_count = 0`), `check_purge`'s step-1 eviction never removes them. If the attacker spreads addresses across diverse /16 subnets (≤ 4 per subnet), step-2 eviction also fails, causing `check_purge` to return `Err(EvictionFailed)`. From that point on, no new legitimate address can be inserted, `fetch_addrs_to_attempt` returns empty, and outbound peer discovery is permanently blocked until the node slowly exhausts feeler-connection retries against all 16 384 sybil entries.

---

### Finding Description

**Entry point — `add_new_addrs` / `add_addr`**

`DiscoveryAddressManager::add_new_addrs` is called directly from `DiscoveryProtocol::received` whenever a `Nodes` message arrives: [1](#0-0) 

It iterates every address and calls `peer_store.add_addr`: [2](#0-1) 

`add_addr` hardcodes `last_connected_at_ms = 0` for every discovered address: [3](#0-2) 

**Per-message limits do not prevent flooding**

`verify_nodes_message` caps a non-announce `Nodes` message at `MAX_ADDR_TO_SEND = 1000` items, each with up to `MAX_ADDRS = 3` addresses — 3 000 addresses per message: [4](#0-3) 

After the first non-announce `Nodes` message, `state.received_nodes` is set and subsequent ones trigger `DuplicateFirstNodes` → disconnect. However, the attacker simply disconnects and reconnects; each new TCP session gets a fresh `SessionState` with `received_nodes = false`. Six reconnections inject ≥ 18 000 unique addresses — enough to saturate the 16 384-entry store.

**`check_purge` step 1 cannot evict fresh sybil addresses**

`is_connectable` for an address with `last_connected_at_ms = 0` and `attempts_count = 0`: [5](#0-4) 

- `tried_in_last_minute` → false (`last_tried_at_ms = 0`)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES (3)` → **false** (count is 0)
- `now_ms.saturating_sub(0) > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES (10)` → second clause **false** (count is 0)

Result: `is_connectable` returns **`true`** for every fresh sybil address. Step 1 of `check_purge` finds zero candidates: [6](#0-5) 

**`check_purge` step 2 cannot evict diverse-subnet sybil addresses**

Step 2 groups addresses by network segment, sorts by group size descending, takes the top half, and only evicts 2 addresses from groups with **> 4 peers**: [7](#0-6) 

If the attacker spreads ≤ 4 addresses per /16 subnet (requiring ≥ 4 096 distinct /16 blocks — trivially achievable by advertising random public IPs), no group exceeds the threshold. `candidate_peers` is empty again, and `check_purge` returns: [8](#0-7) 

**Error is silently swallowed**

`add_addr` propagates the error via `?`. `add_new_addrs` catches it and logs at `debug` level only: [9](#0-8) 

From this point, every call to `add_addr` for a legitimate address also fails silently.

**`fetch_addrs_to_attempt` returns empty**

`fetch_addrs_to_attempt` requires `last_connected_at_ms > now − ADDR_TRY_TIMEOUT_MS (3 days)`: [10](#0-9) 

Sybil addresses have `last_connected_at_ms = 0`, so `0 > (now − 3 days)` is false. The function returns an empty list — the victim node cannot initiate new outbound connections to legitimate peers.

**`fetch_addrs_to_feeler` returns only sybil addresses**

`fetch_addrs_to_feeler` selects addresses that have **not** been connected within 3 days: [11](#0-10) 

All 16 384 sybil entries qualify. The victim wastes all feeler slots on attacker-controlled IPs.

**Self-healing is extremely slow**

After `ADDR_MAX_RETRIES = 3` failed feeler attempts, `is_connectable` returns false for a sybil address and it gets evicted. But feeler connections are rate-limited; clearing 16 384 entries requires 49 152 failed connection attempts, which at any realistic feeler rate takes days to weeks. [12](#0-11) 

---

### Impact Explanation

- **Outbound peer discovery is blocked**: `fetch_addrs_to_attempt` returns empty; the node cannot establish new outbound connections to legitimate peers.
- **Peer store persistence**: The sybil-filled store is saved to disk. On restart, the node boots with 16 384 sybil addresses and zero legitimate ones, making reconnection to the honest network impossible without manual intervention.
- **Feeler connections are hijacked**: All feeler slots are consumed by attacker IPs, preventing verification of any legitimate address.
- **`fetch_random_addrs` propagation is not the primary concern**: The question correctly notes that sybil addresses (with `last_connected_at_ms = 0`) do not pass the 7-day filter in `fetch_random_addrs` and are not re-advertised. The real impact is store saturation, not propagation.

Existing *currently-connected* sessions survive (they live in `connected_peers`, not `addr_manager`), so the node is not instantly isolated. However, after any disconnect or restart, it cannot recover legitimate peers from the store.

---

### Likelihood Explanation

- **No privilege required**: Any peer that can open a TCP connection to the victim's P2P port can execute this.
- **Low cost**: The attacker advertises arbitrary public IPs — they do not need to control them. Six reconnections with crafted `Nodes` messages suffice.
- **No PoW, no key, no majority hashpower**: The attack is purely at the application-layer discovery protocol.
- **Amplification**: A single attacker IP can reconnect repeatedly; there is no per-IP reconnection rate limit visible in the reviewed code.

---

### Recommendation

1. **Rate-limit per-session address ingestion**: Track how many addresses each session has contributed and reject further contributions once a per-session cap is reached (e.g., 500 addresses per session lifetime).
2. **Prefer evicting never-connected addresses in `check_purge`**: In step 1, treat `last_connected_at_ms == 0 && attempts_count == 0` as a lower-priority entry, evicting them before stale-but-tried addresses.
3. **Subnet cap on insertion**: Before calling `addr_manager.add`, count existing entries in the same /16 subnet and reject the new address if the subnet already has ≥ N entries (e.g., N = 4), mirroring the eviction threshold.
4. **Persist `attempts_count` across restarts**: Currently a flooded store survives restarts with all `attempts_count = 0`, resetting the self-healing clock.

---

### Proof of Concept

```
1. Victim node starts with an empty or partially-filled peer store.
2. Attacker opens a P2P connection to victim.
3. Attacker sends DiscoveryMessage::Nodes { announce: false, items: [1000 Node entries,
   each with 3 unique public IPs from distinct /16 subnets] }.
   → add_new_addrs injects 3000 addresses, all with last_connected_at_ms=0.
4. Attacker disconnects and reconnects. Repeat 6 times.
   → peer store reaches 16384 entries.
5. On the 7th batch, check_purge runs:
   - Step 1: all sybil entries have is_connectable=true → 0 evictions.
   - Step 2: all subnets have ≤4 entries → 0 evictions.
   - Returns Err(EvictionFailed).
6. add_addr returns Err; add_new_addrs logs at debug and continues.
7. Assert: peer_store.fetch_addrs_to_attempt(8, required_flags, |_| true) == []
8. Assert: peer_store.addr_manager().count() == 16384 (all sybil)
9. Legitimate peer sends its address via discovery → silently dropped.
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L205-205)
```rust
                                self.addr_mgr.add_new_addrs(session.id, addrs);
```

**File:** network/src/protocols/discovery/mod.rs (L279-288)
```rust
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }
```

**File:** network/src/protocols/discovery/mod.rs (L352-362)
```rust
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

**File:** network/src/peer_store/peer_store_impl.rs (L206-208)
```rust
                && peer_addr
                    .connected(|t| t > addr_expired_ms && t <= now_ms.saturating_sub(DIAL_INTERVAL))
                && required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
```

**File:** network/src/peer_store/peer_store_impl.rs (L230-239)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

        self.addr_manager.fetch_random(count, filter)
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

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
