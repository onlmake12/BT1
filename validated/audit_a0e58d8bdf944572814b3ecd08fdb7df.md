All code claims have been verified against the actual source. Here is the validated audit report:

---

Audit Report

## Title
`check_purge` Integer Division Zero in `take(len / 2)` Allows Peer Store Exhaustion via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

When all addresses in the peer store belong to the same `/16` network group, `check_purge`'s Phase 2 eviction computes `take(1 / 2)` = `take(0)`, selecting zero candidates. The function returns `Err(PeerStoreError::EvictionFailed)`, which propagates out of `add_addr`. An attacker who pre-fills the store with `ADDR_COUNT_LIMIT` (16 384) addresses from a single `/16` permanently prevents the victim node from recording any new peer addresses learned via discovery.

## Finding Description

**Root cause — `take(len / 2)` with `len == 1`:**

`check_purge` is called by `add_addr` before every insertion. [1](#0-0) 

Phase 1 collects addresses where `is_connectable` returns `false` and removes them. Phase 2 is only entered when Phase 1 found nothing: [2](#0-1) 

The critical line:
```rust
peers.into_iter().take(len / 2)
``` [3](#0-2) 

When `len == 1`, Rust integer division yields `1 / 2 == 0`, so `take(0)` produces an empty iterator. `candidate_peers` is empty, and the function returns: [4](#0-3) 

**Why Phase 1 does not evict freshly-added addresses:**

`add_addr` inserts with `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` only returns `false` when `attempts_count >= ADDR_MAX_RETRIES (3)` (never-connected path) or `attempts_count >= ADDR_MAX_FAILURES (10)` (stale-connection path). Fresh addresses satisfy neither condition and survive Phase 1 intact: [5](#0-4) 

**Network group definition — all of `1.2.0.0/16` maps to one group:**

`Group::IP4` uses only the first two octets, so every address in a `/16` block maps to the same group key: [6](#0-5) 

**`ADDR_COUNT_LIMIT` is 16 384:** [7](#0-6) 

**Optional feeler-confirmation hardens the attack:**

If the attacker also accepts feeler connections, `add_outbound_addr` stamps `last_connected_at_ms = unix_time_as_millis()`, making addresses permanently connectable and immune to Phase 1 even after the victim retries: [8](#0-7) 

## Impact Explanation

Once the store is saturated with 16 384 same-`/16` addresses, every subsequent call to `add_addr` returns `EvictionFailed`. The victim node cannot record any new peer addresses from the discovery protocol. If existing peers disconnect, the node cannot replace them and becomes progressively isolated. Scaled to many nodes simultaneously, this constitutes a low-cost mechanism to cause CKB network-wide peer discovery failure and potential network congestion/partition.

**Severity: High (10 001 – 15 000 points)** — "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation

- Requires only **one** malicious peer already connected to the victim (or reachable via the discovery gossip chain) to inject 16 384 `ADDR` records.
- All injected addresses can be from a single `/16` the attacker controls or fabricates — discovery addresses are not verified before being stored.
- No PoW, no privileged role, no key material required.
- The feeler-confirmation step is optional; freshly-added addresses with `attempts_count = 0` already survive Phase 1 eviction.
- The attack is repeatable and permanent until the node is restarted with a cleared peer store.

## Recommendation

Replace `take(len / 2)` with `take((len + 1) / 2)` (ceiling division) so that even a single-group store always has at least one group considered for eviction:

```rust
// Before
peers.into_iter().take(len / 2)

// After
peers.into_iter().take((len + 1) / 2)
```

Additionally, enforce a per-`/16` insertion cap in `addr_manager` (e.g., reject insertion if the group already holds more than `ADDR_COUNT_LIMIT / expected_groups` entries), mirroring Bitcoin Core's bucketed address manager design.

## Proof of Concept

```rust
let mut peer_store = PeerStore::default();

// Fill store with ADDR_COUNT_LIMIT addresses, all in 1.2.0.0/16
for i in 0..ADDR_COUNT_LIMIT {
    let port = 10000 + i as u16;
    let addr: Multiaddr = format!(
        "/ip4/1.2.{}.{}/tcp/{}/p2p/{}",
        (i / 256) % 256, i % 256, port,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);

// Now try to add a new honest peer — must fail
let new_addr: Multiaddr = format!(
    "/ip4/8.8.8.8/tcp/8333/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // EvictionFailed — store is permanently locked
```

`peers_by_network_group.len() == 1`, `take(1/2) == take(0)`, `candidate_peers` is empty, `check_purge` returns `Err(PeerStoreError::EvictionFailed)`. [9](#0-8)

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

**File:** network/src/peer_store/peer_store_impl.rs (L103-114)
```rust
    pub fn add_outbound_addr(&mut self, addr: Multiaddr, flags: Flags) {
        if self.ban_list.is_addr_banned(&addr) {
            return;
        }
        let score = self.score_config.default_score;
        self.addr_manager.add(AddrInfo::new(
            addr,
            ckb_systemtime::unix_time_as_millis(),
            score,
            flags.bits(),
        ));
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L357-401)
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
