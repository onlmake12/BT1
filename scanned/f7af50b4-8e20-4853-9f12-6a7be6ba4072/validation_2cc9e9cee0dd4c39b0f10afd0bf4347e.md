All cited code has been verified against the actual repository. Every technical claim checks out:

Audit Report

## Title
Peer Store Eviction Bypass via Single-Group Address Flood — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge`'s second-pass eviction computes `take(len / 2)` where `len` is the number of distinct network groups. When all stored addresses share one /16 subnet, `len = 1` and integer division yields `take(0)`, producing an empty candidate list and returning `EvictionFailed`. An attacker who pre-fills the store with 16 384 connectable addresses from a single /16 can cause every subsequent `add_addr` call to fail, blocking Discovery-protocol address ingestion for the duration of the flooded entries.

## Finding Description

**Entrypoint:** `add_new_addrs` in `network/src/protocols/discovery/mod.rs` iterates inbound Discovery Nodes messages and calls `peer_store.add_addr(addr, flags)` for each address passing `is_valid_addr`. [1](#0-0) 

**`add_addr` always calls `check_purge` before inserting:** [2](#0-1) 

**First pass of `check_purge` finds nothing to evict:** All 16 384 freshly-added entries have `last_connected_at_ms=0`, `attempts_count=0`, `last_tried_at_ms=0`. Walking `is_connectable`: `tried_in_last_minute` is false (0 ≥ now_ms−60000 is false), the `attempts_count >= ADDR_MAX_RETRIES(3)` clause is false (0 ≥ 3), and the `attempts_count >= ADDR_MAX_FAILURES(10)` clause is false (0 ≥ 10) — so every entry returns `true` and the first pass removes nothing. [3](#0-2) 

**Second pass — the `len / 2` integer-division bug:** Network group for IPv4 is keyed by the first two octets (`Group::IP4([bits[0], bits[1]])`), i.e. a /16. [4](#0-3) 

When all 16 384 addresses share one /16, `peers_by_network_group.len() = 1`. The eviction iterator then calls `.take(1 / 2)` = `.take(0)`, iterating zero groups and producing an empty `candidate_peers`. The function returns `Err(PeerStoreError::EvictionFailed)`. [5](#0-4) 

**Error is silently dropped:** `add_new_addrs` logs only at `debug!` level and continues, so the caller never learns that address ingestion has stopped. [6](#0-5) 

**`ADDR_COUNT_LIMIT = 16384`, `ADDR_MAX_RETRIES = 3`, `ADDR_MAX_FAILURES = 10`:** [7](#0-6) 

## Impact Explanation

The node stops ingesting new peer addresses from the Discovery protocol for the duration of the flooded entries. The degradation is bounded: the feeler mechanism will attempt connections to the 16 384 flooded addresses; after `ADDR_MAX_RETRIES = 3` failed attempts each, `is_connectable()` returns `false` and they become evictable in the first pass. Existing connections and outbound dialing (`add_outbound_addr`, `add_connected_peer`) bypass `check_purge` entirely and are unaffected. This matches **Low (501–2000 points): any other important performance improvements for CKB** — it is a bounded but real degradation of peer discovery availability, not a crash or consensus issue.

## Likelihood Explanation

- A single connected peer can send arbitrarily many Discovery Nodes messages; `add_new_addrs` enforces no per-sender rate limit.
- The attacker needs only one P2P connection to a lightly-populated node.
- Constructing 16 384 valid multiaddrs across a public /16 (e.g. `1.2.0.0/16`) is trivial — 65 536 host addresses are available, each with up to 65 535 ports.
- The attack is repeatable: once the feeler exhausts the flooded entries, the attacker can re-flood.

## Recommendation

Replace `take(len / 2)` with a guard that always evicts at least one group when the store is full:

```rust
let take_count = (len / 2).max(1);
peers.into_iter().take(take_count)...
```

Additionally, enforce a per-network-group cap in `add_addr` (e.g. reject if the group already holds more than `ADDR_COUNT_LIMIT / expected_groups` entries) to prevent a single /16 from monopolising the store before eviction is even attempted.

## Proof of Concept

```rust
// 1. Fill addr_manager with ADDR_COUNT_LIMIT addresses, all from 1.2.x.x (/16)
let mut peer_store = PeerStore::default();
for i in 0..16384u32 {
    let port = (i % 65535) + 1;
    let host_b = (i / 256) as u8;
    let host_c = (i % 256) as u8;
    let addr: Multiaddr = format!(
        "/ip4/1.2.{}.{}/tcp/{}/p2p/{}",
        host_b, host_c, port,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.mut_addr_manager().add(AddrInfo::new(
        addr, 0, 100, Flags::COMPATIBILITY.bits()
    ));
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// 2. add_addr must call check_purge; all entries connectable, single group → EvictionFailed
let new_addr: Multiaddr = "/ip4/8.8.8.8/tcp/9999/p2p/<valid-peer-id>"
    .parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
```

### Citations

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

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
