Audit Report

## Title
`check_purge` `take(len/2)` integer-division zero-take permanently blocks peer store writes when all addresses share one /16 subnet — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
When all `ADDR_COUNT_LIMIT` (16384) stored addresses belong to a single `/16` network group, `peers_by_network_group.len()` equals 1, and `take(len / 2)` evaluates to `take(0)` via integer division, producing an empty candidate list. `check_purge` returns `Err(PeerStoreError::EvictionFailed)`, and `add_addr` propagates this error via `?`, blocking all subsequent peer address additions until the store naturally drains through failed connection attempts.

## Finding Description
`check_purge` in `network/src/peer_store/peer_store_impl.rs` has two eviction stages:

**Stage 1** (lines 341–355): Collects addresses where `is_connectable()` returns `false`. Freshly advertised addresses are created with `last_connected_at_ms = 0` and `attempts_count = 0`. In `is_connectable` (types.rs lines 89–105), neither false-return branch fires: `0 < ADDR_MAX_RETRIES (3)` and `0 < ADDR_MAX_FAILURES (10)`. Stage 1 yields nothing.

**Stage 2** (lines 357–401): Groups all addresses by `/16` network segment using `Group::IP4([bits[0], bits[1]])` (network_group.rs lines 26–28). When all 16384 addresses share one `/16` subnet, `peers_by_network_group.len() == 1`. The code then executes:

```rust
let len = peers_by_network_group.len();  // 1
peers.into_iter().take(len / 2)          // take(0) — integer division
```

`take(0)` produces an empty iterator. `candidate_peers` is empty. The function reaches line 399–401:

```rust
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
```

`add_addr` (lines 71–80) propagates this via `?` on line 75, returning `Err` to all callers. The discovery protocol's `add_new_addrs` (discovery/mod.rs lines 347–363) silently logs the error and continues, meaning the node does not crash but permanently fails to register any new peer addresses until existing entries age out through failed connection attempts (requiring `ADDR_MAX_RETRIES = 3` failures per address, or `ADDR_TIMEOUT_MS = 7 days` elapsed).

Note: even if `take(len/2)` were corrected to `take(1)`, the single group of 16384 addresses satisfies `addrs.len() > 4`, so 2 random peers would be evicted — confirming the fix is straightforward.

## Impact Explanation
The targeted node's peer store is saturated and cannot accept new peer addresses. The node cannot discover or connect to new honest peers via the discovery protocol. If existing connections drop, the node risks network isolation. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — a single unauthenticated attacker can isolate one or more nodes with minimal resources, and if applied at scale, degrades the peer discovery layer network-wide.

## Likelihood Explanation
The discovery protocol imposes no per-peer limit on total addresses advertised across multiple messages. A single `Nodes` (announce=false) message can carry up to `MAX_ADDR_TO_SEND = 1000` nodes × `MAX_ADDRS = 3` addresses = 3000 addresses per message. Filling 16384 slots requires approximately 6 such messages from one malicious peer. No proof-of-work, key material, or special privilege is required. The condition is stable: once filled with connectable same-group addresses, the store remains blocked until the node exhausts `ADDR_MAX_RETRIES` connection attempts against all 16384 entries, which may take days under normal feeler connection rates.

## Recommendation
Replace `take(len / 2)` with a ceiling-division formulation that always processes at least one group when `len >= 1`:

```rust
// Option A: ceiling division
peers.into_iter().take(len.saturating_add(1) / 2)

// Option B: explicit minimum
peers.into_iter().take((len / 2).max(1))
```

Additionally, enforce a per-`/16`-group cap during insertion in `add_addr` to prevent any single subnet from monopolizing the store.

## Proof of Concept
Direct unit test against `PeerStore` (bypasses `is_valid_addr` filtering in the discovery layer):

```rust
#[test]
fn test_check_purge_single_group_blocks_add_addr() {
    let mut peer_store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT connectable addresses, all in 1.0.x.x (/16)
    for i in 0..ADDR_COUNT_LIMIT {
        let addr: Multiaddr = format!(
            "/ip4/1.0.{}.{}/tcp/1000/p2p/{}",
            (i / 256) % 256, i % 256,
            PeerId::random().to_base58()
        ).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
    assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);

    // Attempt to add an honest peer from a different subnet
    let honest: Multiaddr = format!(
        "/ip4/192.168.1.1/tcp/1000/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    let result = peer_store.add_addr(honest, Flags::COMPATIBILITY);

    // Bug: EvictionFailed, store unchanged, honest peer rejected
    assert!(result.is_err());
    assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
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

**File:** network/src/network_group.rs (L26-28)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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
