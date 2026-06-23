### Title
Peer Store Permanently Blocked via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

An unprivileged remote peer can permanently block new peer discovery on a CKB node by flooding the peer store with 16,384 addresses all belonging to the same `/16` network group. A mathematical flaw in `check_purge` causes `take(len/2)` to evaluate to `take(0)` when only one network group exists, making eviction impossible and returning `EvictionFailed` for all subsequent `add_addr` calls.

---

### Finding Description

**Root cause — `check_purge` integer division flaw:** [1](#0-0) 

When all stored addresses belong to a single `/16` group, `peers_by_network_group.len()` equals `1`. The expression `take(len / 2)` becomes `take(0)` due to integer division, so the iterator yields nothing. The `addrs.len() > 4` branch is never reached, `candidate_peers` remains empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

**Network group assignment — `/16` granularity:** [2](#0-1) 

All IPv4 addresses sharing the same first two octets (e.g., `1.2.0.0/16`) map to the same `Group::IP4([bits[0], bits[1]])`. A `/16` block contains 65,536 unique IPs, more than enough to supply 16,384 distinct addresses.

**Newly added addresses are connectable — step 1 of `check_purge` cannot evict them:** [3](#0-2) 

`AddrInfo::new` sets `last_connected_at_ms = 0` and `attempts_count = 0`. Since `0 < ADDR_MAX_RETRIES (3)`, `is_connectable` returns `true` for all freshly injected addresses. Step 1 of `check_purge` (evict non-connectable peers) removes nothing, forcing execution into the broken network-group eviction path.

**`add_addr` is the gated entry point:** [4](#0-3) 

`check_purge` is called before every insertion. Once it returns `EvictionFailed`, no new address can ever be added.

**Errors are silently swallowed in `add_new_addrs`:** [5](#0-4) 

The `EvictionFailed` error is only `debug!`-logged. Honest peers' addresses are silently dropped with no operator alert.

**No per-session or per-IP address rate limit exists:** [6](#0-5) 

`add_new_addrs` has no per-session counter, no per-source-IP cap, and no cooldown. The only guard is `is_valid_addr`, which passes for any globally routable IP.

**Message-level limits are insufficient:** [7](#0-6) 

Announce messages allow up to 10 items × 3 addresses = 30 addresses per message. Filling 16,384 slots requires ~547 announce messages from a single connection — trivially achievable.

**`ADDR_COUNT_LIMIT` constant:** [8](#0-7) 

---

### Impact Explanation

Once the peer store is saturated with single-group addresses, the node cannot add any new peer addresses discovered via the P2P discovery protocol. The node's peer store is permanently frozen (until restart or until existing entries expire after 7 days via `ADDR_TIMEOUT_MS`). This prevents the node from learning about new honest peers, degrading its ability to maintain a healthy outbound connection set and participate in block/transaction relay.

---

### Likelihood Explanation

- Requires only one inbound P2P connection (no special privileges).
- ~547 small announce messages suffice; no bandwidth amplification or PoW needed.
- The `/16` block provides 65,536 unique IPs — far more than the 16,384 needed.
- The effect persists for up to 7 days without a node restart.
- The existing test `test_eviction` in `network/src/tests/peer_store.rs` (lines 496–590) already demonstrates the exact same fill pattern with `225.0.0.1` addresses, confirming the scenario is reachable. [9](#0-8) 

---

### Recommendation

Fix the `take(len / 2)` expression in `check_purge`. When `len == 1`, the correct behavior is to evict from that single group (it is the largest group by definition). Replace:

```rust
.take(len / 2)
```

with:

```rust
.take(len.saturating_sub(1).max(1))
// or simply: .take((len / 2).max(1))
```

Additionally, add a per-session or per-source-IP cap on the number of addresses accepted via discovery to limit the rate at which any single peer can populate the store.

---

### Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill with 16384 unique addresses from the same /16 group
for i in 0..ADDR_COUNT_LIMIT {
    let ip2 = (i / 256) as u8;
    let ip3 = (i % 256) as u8;
    let addr: Multiaddr = format!(
        "/ip4/1.2.{}.{}/tcp/8115/p2p/{}",
        ip2, ip3,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
// Now try to add an honest peer from a different /16
let honest: Multiaddr = format!(
    "/ip4/8.8.8.8/tcp/8115/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
// Returns Err(EvictionFailed) — honest peer permanently blocked
assert!(peer_store.add_addr(honest, Flags::COMPATIBILITY).is_err());
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

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
```rust
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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

**File:** network/src/protocols/discovery/mod.rs (L266-299)
```rust
fn verify_nodes_message(nodes: &Nodes) -> Option<Misbehavior> {
    let mut misbehavior = None;
    if nodes.announce {
        if nodes.items.len() > ANNOUNCE_THRESHOLD {
            warn!(
                "Number of nodes exceeds announce threshold {}",
                ANNOUNCE_THRESHOLD
            );
            misbehavior = Some(Misbehavior::TooManyItems {
                announce: nodes.announce,
                length: nodes.items.len(),
            });
        }
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

    if misbehavior.is_none() {
        for item in &nodes.items {
            if item.addresses.len() > MAX_ADDRS {
                misbehavior = Some(Misbehavior::TooManyAddresses(item.addresses.len()));
                break;
            }
        }
    }

    misbehavior
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/tests/peer_store.rs (L496-510)
```rust
fn test_eviction() {
    let mut peer_store = PeerStore::default();
    let now = ckb_systemtime::unix_time_as_millis();
    let tried_ms = now - 61_000;
    // add addrs, make the peer store has 4 groups addrs
    for i in 0..(ADDR_COUNT_LIMIT - 5) {
        let addr: Multiaddr = format!(
            "/ip4/225.0.0.1/tcp/{}/p2p/{}",
            i,
            PeerId::random().to_base58()
        )
        .parse()
        .unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
```
