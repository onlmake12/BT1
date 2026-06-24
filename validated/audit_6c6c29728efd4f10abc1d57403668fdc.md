Audit Report

## Title
Peer Store Permanently Blocked by Single-/16-Subnet Flood via `check_purge` Integer-Division Zero-Take — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` groups stored addresses by `/16` network segment and selects `take(len / 2)` groups for eviction. When all 16 384 slots are filled with addresses from a single `/16` subnet, `len = 1` and Rust integer division yields `1 / 2 = 0`, so `take(0)` produces an empty iterator. `candidate_peers` is empty, the function returns `PeerStoreError::EvictionFailed`, and every subsequent `add_addr` call is permanently blocked via the `?` propagation. A single connected peer can trigger this in seconds using the discovery protocol.

## Finding Description

**Step 1 of `check_purge` — non-connectable eviction (lines 341–355):**
Fresh addresses inserted via `add_addr` are created with `last_connected_at_ms = 0`, `attempts_count = 0`, and `last_tried_at_ms = 0`. `is_connectable` returns `true` for all of them: `tried_in_last_minute` is false (timestamp 0 is far in the past), the `attempts_count >= ADDR_MAX_RETRIES` branch requires `attempts_count >= 3`, and the `ADDR_MAX_FAILURES` branch requires `attempts_count >= 10`. No candidates are found; `candidate_peers` is empty and the code falls through to step 2. [1](#0-0) 

**Step 2 — network-group eviction (lines 358–401):**
All addresses from `225.0.x.x` map to `Group::IP4([225, 0])`, collapsing `peers_by_network_group` to a single key. `len = 1`. The expression `take(len / 2)` evaluates to `take(0)` under Rust integer truncation, yielding an empty iterator. The inner `addrs.len() > 4` guard is never reached. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. [2](#0-1) 

**`add_addr` propagates the error unconditionally:** [3](#0-2) 

**Network group definition — IPv4 /16:** [4](#0-3) 

**`ADDR_COUNT_LIMIT` constant:** [5](#0-4) 

**Attacker entry point — discovery `add_new_addrs`:** [6](#0-5) 

**Identify protocol equally blocked:** [7](#0-6) 

**Existing test masks the regression** by always seeding at least 4 distinct groups before the store is full (`len = 4`, `take(2)` works), so the single-group path is never exercised: [8](#0-7) 

## Impact Explanation

Once the store is saturated with single-group addresses, every `add_addr` call returns `EvictionFailed`. The node cannot record any new peer addresses from discovery or identify. If the attacker's addresses are unreachable, the node exhausts its candidate pool and cannot form new outbound connections, effectively isolating it from the honest network. If the attacker operates reachable servers at those addresses, the node connects exclusively to attacker-controlled peers, enabling an eclipse attack. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as a single attacker can isolate arbitrary nodes with negligible resources, and at scale this degrades the connectivity of the entire CKB P2P network.

## Likelihood Explanation

- Requires only a single connected peer; no privileged role, no PoW, no key material.
- `MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` addresses = 3 000 addresses per `Nodes` message; six messages saturate the 16 384-slot store.
- The discovery protocol imposes no per-subnet admission limit before the store is full.
- The attack is self-sustaining: as addresses age out via step 1 of `check_purge` (after `ADDR_MAX_RETRIES` failed dials), the attacker re-floods via periodic announce messages.
- The `take(len / 2)` path is reached whenever no non-connectable addresses exist, which is the normal state for a freshly flooded store.

## Recommendation

Replace `take(len / 2)` with `take((len / 2).max(1))` so that when there is exactly one group it is still selected for eviction:

```rust
peers
    .into_iter()
    .take((len / 2).max(1))   // was: take(len / 2)
```

Additionally, enforce a per-`/16`-subnet cap inside `add_addr` (e.g., reject an address if its group already holds more than `ADDR_COUNT_LIMIT / N` entries) to prevent any single subnet from monopolising the store before eviction is even attempted.

## Proof of Concept

```rust
#[test]
fn test_single_group_eviction_blocked() {
    let mut peer_store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT addresses, all from 225.0.x.x (/16 group)
    for i in 0..ADDR_COUNT_LIMIT {
        let addr: Multiaddr = format!(
            "/ip4/225.0.{}.{}/tcp/8114/p2p/{}",
            (i >> 8) & 0xff,
            i & 0xff,
            PeerId::random().to_base58()
        ).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
    // Attempt to add one address from a different subnet — must succeed but returns EvictionFailed
    let new_addr: Multiaddr = format!(
        "/ip4/1.2.3.4/tcp/8114/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_ok()); // FAILS
}
```

The test fails because `check_purge` returns `Err(EvictionFailed)` when `len = 1` and `take(0)` yields no eviction candidates.

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L366-401)
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/protocols/discovery/mod.rs (L347-362)
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
```

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
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
