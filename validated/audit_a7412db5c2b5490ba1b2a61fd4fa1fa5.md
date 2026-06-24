All cited code has been verified against the actual repository. Every claim checks out:

- `check_purge` at line 376 uses `.take(len / 2)` — confirmed.
- When all addresses share one `/16` group, `len == 1` and `1 / 2 == 0` in integer arithmetic, so `.take(0)` yields no candidates.
- `AddrInfo::new` sets `last_connected_at_ms = 0` and `attempts_count = 0` — confirmed.
- `is_connectable` returns `true` for such entries (condition `attempts_count >= ADDR_MAX_RETRIES` is `0 >= 3` = false) — confirmed.
- Step 1 of `check_purge` removes nothing; execution falls into the broken group-eviction path.
- `add_new_addrs` only emits `debug!` on `EvictionFailed` — confirmed.
- `_session_id` is unused; no per-session cap — confirmed.
- `ADDR_COUNT_LIMIT = 16384` — confirmed.
- `test_eviction` deliberately adds 3 addresses from different groups before triggering eviction, avoiding the single-group case — confirmed.

---

Audit Report

## Title
Integer Division Truncation in `check_purge` Allows Single-Group Address Flooding to Permanently Freeze Peer Store — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
When all 16,384 peer store slots are filled with addresses from a single `/16` network group, `check_purge` computes `.take(len / 2)` where `len == 1`, yielding `.take(0)` due to integer truncation. This produces an empty eviction candidate list and unconditionally returns `Err(PeerStoreError::EvictionFailed)`, permanently blocking all subsequent `add_addr` calls until node restart or 7-day address expiry. A single unprivileged inbound connection can trigger this via repeated discovery announce messages.

## Finding Description

**Root cause — integer division truncation:**
In `network/src/peer_store/peer_store_impl.rs` at line 366–376, `peers_by_network_group.len()` equals `1` when all stored addresses share one `/16` group. The expression `.take(len / 2)` becomes `.take(1 / 2)` = `.take(0)`, so the iterator yields no groups. The `addrs.len() > 4` branch is never evaluated, `candidate_peers` remains empty, and lines 399–401 return `Err(PeerStoreError::EvictionFailed)`. [1](#0-0) 

**Network group granularity — `/16` blocks:**
`network/src/network_group.rs` lines 26–28 map all IPv4 addresses sharing the same first two octets to `Group::IP4([bits[0], bits[1]])`. A single `/16` block contains 65,536 unique IPs, far exceeding the 16,384 slots needed to saturate the store. [2](#0-1) 

**Step 1 evicts nothing — freshly injected addresses are always connectable:**
`add_addr` calls `AddrInfo::new(addr, 0, score, flags.bits())`, setting `last_connected_at_ms = 0` and `attempts_count = 0`. In `is_connectable`, the condition `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES` evaluates to `false` (since `0 < 3`), so the function returns `true`. Step 1 of `check_purge` removes nothing, forcing execution into the broken group-eviction path. [3](#0-2) [4](#0-3) 

**`EvictionFailed` is silently swallowed:**
`add_new_addrs` in `network/src/protocols/discovery/mod.rs` only emits a `debug!` log on failure. No operator alert, no misbehavior score, no session disconnect. [5](#0-4) 

**No per-session or per-source-IP rate limit:**
`add_new_addrs` accepts `_session_id` but ignores it entirely. `is_valid_addr` only checks global routability. There is no per-session counter or per-source-IP cap. [6](#0-5) 

**`ADDR_COUNT_LIMIT` constant:** [7](#0-6) 

**Existing test confirms the fill pattern is reachable but does not cover the single-group case:**
`test_eviction` fills the store with `ADDR_COUNT_LIMIT - 5` addresses all from `225.0.x.x` (same `/16`), then adds 3 addresses from different groups before triggering eviction. This ensures `len >= 4` and `len / 2 >= 2`, deliberately avoiding the single-group scenario. [8](#0-7) 

## Impact Explanation

Once the peer store is saturated with single-group addresses, the node cannot add any new peer addresses discovered via the P2P discovery protocol. The peer store is frozen for up to 7 days (`ADDR_TIMEOUT_MS`). Applied across many nodes simultaneously — each requiring only one inbound connection — this degrades the entire network's peer discovery mesh. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation

- Requires only one inbound P2P connection — no special privileges.
- A single `/16` block provides 65,536 unique IPs, far more than the 16,384 needed.
- The effect persists for up to 7 days without a node restart.
- `EvictionFailed` is silently swallowed, so neither the operator nor the protocol detects the freeze.
- The attack is repeatable and scriptable with minimal bandwidth.

## Recommendation

Replace the truncating integer division in `check_purge`:

```rust
// Before (line 376):
.take(len / 2)

// After:
.take((len / 2).max(1))
```

This ensures that when only one network group exists, it is still selected for eviction (it is by definition the largest group). Additionally, add a per-session cap on the number of addresses accepted via discovery to limit the rate at which any single peer can populate the store.

## Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill with 16384 unique addresses from the same /16 group (1.2.x.x)
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
// Attempt to add an honest peer from a different /16
let honest: Multiaddr = format!(
    "/ip4/8.8.8.8/tcp/8115/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
// Returns Err(EvictionFailed) — peer store permanently frozen
assert!(peer_store.add_addr(honest, Flags::COMPATIBILITY).is_err());
```

### Citations

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

**File:** network/src/network_group.rs (L26-28)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
```

**File:** network/src/peer_store/types.rs (L65-76)
```rust
    pub fn new(addr: Multiaddr, last_connected_at_ms: u64, score: Score, flags: u64) -> Self {
        AddrInfo {
            // only store tcp protocol
            addr: base_addr(&addr),
            score,
            last_connected_at_ms,
            last_tried_at_ms: 0,
            attempts_count: 0,
            random_id_pos: 0,
            flags,
        }
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
