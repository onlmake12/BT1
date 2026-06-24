Audit Report

## Title
Peer Store Permanently Locked by Adversarial Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
The `check_purge` function in `peer_store_impl.rs` contains an off-by-one condition (`addrs.len() > 4`) in its phase-2 eviction logic. An attacker who fills the peer store with exactly 4 addresses per /16 subnet causes both eviction phases to yield zero candidates, permanently returning `EvictionFailed` on every subsequent `add_addr` call. The error is silently swallowed at `debug!` level, leaving the node unable to record any new peer addresses.

## Finding Description

`check_purge` is invoked by `add_addr` whenever `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384).

**Phase 1** (lines 341–355): collects addresses where `!is_connectable(now_ms)` and removes them. Addresses inserted via `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0` (line 78, `AddrInfo::new(addr, 0, score, flags.bits())`). For such an address, `is_connectable` checks:
- `tried_in_last_minute`: false (`last_tried_at_ms = 0`)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES (3)`: false (`0 >= 3` is false)
- `now_ms.saturating_sub(0) > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES (10)`: false (`0 >= 10` is false)

So `is_connectable` returns `true` for all freshly-added addresses, and phase 1 finds zero candidates.

**Phase 2** (lines 358–401): groups addresses by `/16` network group (`Group::IP4([bits[0], bits[1]])`), sorts groups by size descending, takes the top `len/2` groups, and for each group applies:

```rust
if addrs.len() > 4 {   // line 378
    Some(...)
} else {
    None
}
```

If the attacker fills the store with exactly 4096 distinct /16 subnets × 4 addresses each = 16384 total, every group has `len == 4`. The condition `4 > 4` is `false`, so `flat_map` yields `None` for every group. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

In `add_new_addrs` (lines 355–360), this error is caught and logged only at `debug!` level — it is never propagated to the caller or the operator.

Every subsequent `add_addr` call hits the same dead end: the store remains at 16384 attacker-controlled entries, and no legitimate address can ever be inserted.

## Impact Explanation

Once the store is locked, `fetch_addrs_to_attempt` requires `last_connected_at_ms > addr_expired_ms` (within 3 days); attacker addresses have `last_connected_at_ms = 0` so they are never returned for outbound attempts. `fetch_addrs_to_feeler` returns addresses not connected within 3 days, so it will return attacker addresses — all of which are unresponsive. The node exhausts its feeler budget on dead addresses, cannot discover honest peers, and after existing connections drop, becomes isolated from the honest network. An isolated node cannot receive new blocks and diverges from the chain tip, constituting **consensus deviation**. This matches: **High — Vulnerabilities which could easily crash a CKB node** (network isolation leading to inability to sync), with potential escalation to Critical if combined with active disconnection of existing peers.

## Likelihood Explanation

The attack requires only P2P connectivity. Any peer connected to the victim can send `Nodes` messages via the discovery protocol. `MAX_ADDR_TO_SEND = 1000` addresses per message means ~17 messages suffice to fill 16384 slots. The attacker needs globally routable IPv4 addresses to pass `is_valid_addr` / `is_reachable`, but these are trivially obtainable (real attacker-controlled IP space or BGP-announced prefixes). The condition is deterministic and reproducible: the exact distribution (4 per /16) is easy to construct and the lockout is permanent across node uptime.

## Recommendation

Change the eviction threshold from strictly-greater-than to greater-than-or-equal-to-2, so any group with at least 2 entries is eligible for eviction:

```rust
// current (vulnerable):
if addrs.len() > 4 {

// fixed:
if addrs.len() >= 2 {
```

Additionally, add a hard fallback: if `candidate_peers` is still empty after phase 2 (e.g., every group has exactly 1 entry), evict the single entry with the oldest `last_tried_at_ms` to guarantee `check_purge` never returns `EvictionFailed` when the store is full of connectable addresses.

## Proof of Concept

```rust
#[test]
fn test_adversarial_eviction_lockout() {
    let mut peer_store = PeerStore::default();
    // Fill with 16384 addresses: 4096 /16 subnets × 4 addresses each
    let mut count = 0u32;
    'outer: for a in 1u8..=255 {
        for b in 0u8..=255 {
            for port in 1u16..=4 {
                if count >= 16384 { break 'outer; }
                let addr: Multiaddr = format!(
                    "/ip4/{}.{}.1.1/tcp/{}/p2p/{}",
                    a, b, port, PeerId::random().to_base58()
                ).parse().unwrap();
                peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
                count += 1;
            }
        }
    }
    assert_eq!(peer_store.addr_manager().count(), 16384);

    // Any subsequent add_addr must fail with EvictionFailed
    let new_addr: Multiaddr = format!(
        "/ip4/10.0.0.1/tcp/9999/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
    assert!(result.is_err()); // EvictionFailed — store permanently locked
    assert_eq!(peer_store.addr_manager().count(), 16384);
}
```

The test directly exercises the off-by-one: after filling the store with the adversarial distribution, every call to `add_addr` returns `Err(EvictionFailed)` and the count stays at 16384. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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
