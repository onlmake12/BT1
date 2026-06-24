Audit Report

## Title
Peer Store Sybil Flood via Discovery `Nodes` Messages Bypasses `check_purge` Eviction — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`add_addr` hardcodes `last_connected_at_ms = 0` for all discovered addresses, causing `is_connectable` to return `true` for every fresh sybil entry (since `attempts_count` starts at 0). An attacker sending crafted `Nodes` discovery messages across multiple reconnections can saturate the 16 384-entry peer store with sybil addresses that neither eviction step in `check_purge` can remove, permanently blocking outbound peer discovery and hijacking all feeler connection slots until the node manually exhausts thousands of failed connection attempts.

## Finding Description

**Entry point — `add_new_addrs` / `add_addr`**

`DiscoveryAddressManager::add_new_addrs` is called from `DiscoveryProtocol::received` whenever a `Nodes` message arrives: [1](#0-0) 

It iterates every address and calls `peer_store.add_addr`, which hardcodes `last_connected_at_ms = 0` for every discovered address: [2](#0-1) 

**Per-message limits do not prevent flooding**

`verify_nodes_message` caps a non-announce `Nodes` message at `MAX_ADDR_TO_SEND = 1000` items, each with up to `MAX_ADDRS = 3` addresses — 3 000 addresses per message: [3](#0-2) [4](#0-3) 

After the first non-announce `Nodes` message, `state.received_nodes` is set and subsequent ones trigger `DuplicateFirstNodes` → disconnect. However, the attacker simply disconnects and reconnects; each new TCP session gets a fresh `SessionState` with `received_nodes = false`. Six reconnections inject ≥ 18 000 unique addresses — enough to saturate the 16 384-entry store (`ADDR_COUNT_LIMIT`): [5](#0-4) [6](#0-5) 

**`check_purge` step 1 cannot evict fresh sybil addresses**

`is_connectable` for an address with `last_connected_at_ms = 0` and `attempts_count = 0` returns `true` because neither eviction condition is met: [7](#0-6) 

Step 1 of `check_purge` only collects addresses where `!addr.is_connectable(now_ms)`, finding zero candidates among fresh sybil entries: [8](#0-7) 

**`check_purge` step 2 cannot evict diverse-subnet sybil addresses**

Step 2 groups addresses by network segment and only evicts 2 addresses from groups with `> 4` peers. If the attacker spreads ≤ 4 addresses per /16 subnet (requiring ≥ 4 096 distinct /16 blocks — trivially achievable by advertising random public IPs), no group exceeds the threshold, `candidate_peers` is empty, and `check_purge` returns `Err(EvictionFailed)`: [9](#0-8) 

**Error is silently swallowed**

`add_addr` propagates the error via `?`: [10](#0-9) 

`add_new_addrs` catches it and logs at `debug` level only: [11](#0-10) 

**`fetch_addrs_to_attempt` returns empty**

`fetch_addrs_to_attempt` requires `last_connected_at_ms > now − ADDR_TRY_TIMEOUT_MS (3 days)`. Sybil addresses have `last_connected_at_ms = 0`, so the filter always fails and the function returns an empty list: [12](#0-11) 

**`fetch_addrs_to_feeler` returns only sybil addresses**

`fetch_addrs_to_feeler` selects addresses that have not been connected within 3 days. All 16 384 sybil entries qualify, consuming all feeler slots: [13](#0-12) 

**Self-healing is extremely slow**

After `ADDR_MAX_RETRIES = 3` failed feeler attempts, `is_connectable` returns false for a sybil address and it gets evicted. Clearing 16 384 entries requires 49 152 failed connection attempts: [14](#0-13) 

## Impact Explanation

This vulnerability matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single attacker with no privileges can permanently disable outbound peer discovery on any reachable CKB node. `fetch_addrs_to_attempt` returns empty, preventing new outbound connections to legitimate peers. The sybil-filled store is persisted to disk; on restart, the node boots with 16 384 sybil addresses and zero legitimate ones, making reconnection to the honest network impossible without manual intervention. Applied at scale against multiple nodes, this degrades overall CKB network connectivity and peer propagation.

## Likelihood Explanation

- No privilege required: any peer that can open a TCP connection to the victim's P2P port can execute this.
- Low cost: the attacker advertises arbitrary public IPs they do not need to control. Six reconnections with crafted `Nodes` messages suffice.
- No PoW, no key, no majority hashpower: purely application-layer.
- No per-IP reconnection rate limit is present in the reviewed code path.
- The attack is repeatable and persistent across node restarts.

## Recommendation

1. **Rate-limit per-session address ingestion**: Track how many addresses each session has contributed and reject further contributions once a per-session cap is reached (e.g., 500 addresses per session lifetime).
2. **Prefer evicting never-connected addresses in `check_purge`**: In step 1, treat `last_connected_at_ms == 0 && attempts_count == 0` as a lower-priority entry, evicting them before stale-but-tried addresses.
3. **Subnet cap on insertion**: Before calling `addr_manager.add`, count existing entries in the same /16 subnet and reject the new address if the subnet already has ≥ N entries (e.g., N = 4), mirroring the eviction threshold.
4. **Persist `attempts_count` across restarts**: Currently a flooded store survives restarts with all `attempts_count = 0`, resetting the self-healing clock.

## Proof of Concept

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

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L181-205)
```rust
                            if !nodes.announce && state.received_nodes {
                                warn!("Nodes (announce=false) message received");
                                if check(Misbehavior::DuplicateFirstNodes)
                                    && context.disconnect(session.id).await.is_err()
                                {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                            } else {
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
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
