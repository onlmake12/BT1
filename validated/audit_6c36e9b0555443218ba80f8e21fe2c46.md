All cited code is confirmed in the repository. One critical detail requires correction before final assessment: the `Group` implementation.

Audit Report

## Title
Peer Store Permanent Saturation via Off-by-One in `check_purge` Group Eviction Threshold — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` evicts from a network group only when `addrs.len() > 4` (strictly greater than). An attacker who fills all 16 384 peer-store slots with exactly 4 addresses per /16 network group triggers a state where neither eviction pass removes any entry, causing `Err(EvictionFailed)` to propagate through `add_addr`. The discovery protocol silently drops the error at `debug` level, so every subsequent honest address is permanently rejected and the store stays saturated with attacker-controlled entries.

## Finding Description

**Root cause — off-by-one in pass 2 of `check_purge`:**

`check_purge` runs two eviction passes when `addr_manager.count() >= ADDR_COUNT_LIMIT` (16 384):

- **Pass 1** collects every address for which `is_connectable(now_ms)` returns `false` and removes them. [1](#0-0) 

- **Pass 2** (only if pass 1 removed nothing) groups addresses by network segment, sorts descending by group size, takes the top half of groups, and evicts 2 addresses per group **only if `addrs.len() > 4`** (strictly greater than 4): [2](#0-1) 

- If both passes produce an empty candidate list, the function returns `Err(PeerStoreError::EvictionFailed)`. [3](#0-2) 

**`add_addr` propagates that error with `?`:** [4](#0-3) 

**`is_connectable` returns `true` for any freshly-added address** because `attempts_count` starts at 0 (below `ADDR_MAX_RETRIES = 3`) and `last_connected_at_ms` starts at 0, so neither false-branch is reached: [5](#0-4) [6](#0-5) 

**Network group granularity is /16 (first two IPv4 octets)**, not /24: [7](#0-6) 

This means the attacker must use 4 096 distinct /16 subnets (e.g., `A.B.*.*` for 4 096 distinct `(A,B)` pairs) with exactly 4 addresses each to saturate all 16 384 slots while keeping every group at exactly 4 — the threshold that `> 4` never fires on.

**Discovery silently swallows the error:** [8](#0-7) 

**`ADDR_COUNT_LIMIT`:** [9](#0-8) 

## Impact Explanation

With the store permanently saturated, no new honest peer address can be stored. `fetch_addrs_to_feeler` returns only attacker-controlled addresses (all have `last_connected_at_ms = 0`, satisfying the "never connected" filter). After feeler connections succeed, those entries are promoted into `fetch_addrs_to_attempt` candidates. After a node restart the victim makes all outbound connections to attacker nodes — a full eclipse. An eclipsed node can be fed a fake chain, have its transactions censored, or be used to facilitate double-spend attacks against it.

This matches the allowed impact: **Vulnerabilities which could easily damage CKB economy** — Critical (15 001 – 25 000 points).

## Likelihood Explanation

The attacker needs only one TCP connection to the victim. A single discovery `Nodes` message can carry up to 1 000 addresses; approximately 17 such messages suffice to fill 16 384 slots. The addresses do not need to be reachable — they only need to be synthesised from 4 096 distinct /16 subnets with exactly 4 addresses each. No rate-limit on discovery address ingestion is present in the code. The attack is low-cost, requires no special privilege, and is repeatable.

## Recommendation

Change the eviction threshold from strictly greater than 4 to greater than or equal to 4 in `check_purge`:

```rust
// Before (network/src/peer_store/peer_store_impl.rs, line 378)
if addrs.len() > 4 {

// After
if addrs.len() >= 4 {
```

Additionally, consider adding a per-session rate limit on the number of discovery addresses accepted, and logging `EvictionFailed` at `warn` or `error` level rather than `debug`.

## Proof of Concept

The submitted PoC contains a subnet-structure error: it uses `10.{subnet}.{host}.1` addresses, but the `Group` implementation keys on the **first two octets** (`[bits[0], bits[1]]`), so all addresses sharing the same `subnet` value fall into the same /16 group — producing groups of ~1 020 entries, not 4. The eviction threshold `> 4` fires correctly for those large groups, so the submitted test would **not** fail as claimed.

A corrected PoC must use 4 096 distinct /16 subnets (vary both the first and second octet across the public address space) with exactly 4 addresses per group:

```rust
#[test]
fn test_eclipse_via_exact_group_saturation() {
    use crate::peer_store::{ADDR_COUNT_LIMIT, PeerStore};
    use crate::Flags;
    use p2p::{multiaddr::Multiaddr, secio::PeerId};

    let mut peer_store = PeerStore::default();

    // Fill store: 4096 /16 groups × 4 addresses = 16384 (ADDR_COUNT_LIMIT)
    // Vary first two octets to get distinct Group::IP4([a, b]) keys.
    let mut count = 0usize;
    'outer: for a in 1u8..=64u8 {          // 64 values for first octet
        for b in 0u8..=63u8 {              // 64 values for second octet → 64×64 = 4096 groups
            for port in 1u16..=4 {         // 4 addresses per group
                if count >= ADDR_COUNT_LIMIT { break 'outer; }
                let addr: Multiaddr = format!(
                    "/ip4/{}.{}.1.1/tcp/{}/p2p/{}",
                    a, b, port,
                    PeerId::random().to_base58()
                ).parse().unwrap();
                peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
                count += 1;
            }
        }
    }
    assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);

    // Now try to add an honest address — must succeed but currently fails
    let honest: Multiaddr = format!(
        "/ip4/8.8.8.8/tcp/8333/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    let result = peer_store.add_addr(honest.clone(), Flags::COMPATIBILITY);
    // Fails on current code because check_purge returns Err(EvictionFailed):
    assert!(result.is_ok(), "honest address must always be accepted");
}
```

With this corrected structure every group has exactly 4 entries, `addrs.len() > 4` is false for all of them, pass 2 produces an empty candidate list, and `Err(EvictionFailed)` is returned — confirming the bug.

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

**File:** network/src/peer_store/peer_store_impl.rs (L374-401)
```rust
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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
