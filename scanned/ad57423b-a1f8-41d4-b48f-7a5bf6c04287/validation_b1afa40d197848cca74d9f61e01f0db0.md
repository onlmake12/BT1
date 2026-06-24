Audit Report

## Title
Peer Store Permanently Locked by Adversarial Address Distribution via Off-by-One in `check_purge` Phase 2 Eviction — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` in `peer_store_impl.rs` contains an off-by-one guard (`addrs.len() > 4`) in its phase-2 eviction path. When an attacker fills all 16384 peer store slots with IPv4 addresses distributed uniformly across /16 subnets at exactly 4 addresses per subnet, phase 1 finds no non-connectable entries and phase 2 produces an empty eviction candidate list, causing every subsequent `add_addr` call to return `EvictionFailed`. The error is silently swallowed by the discovery layer, permanently preventing the node from recording any new peer addresses and starving it of outbound connection candidates.

## Finding Description

`check_purge` is invoked by `add_addr` whenever `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384). [1](#0-0) 

**Phase 1** collects all addresses for which `!is_connectable(now_ms)` and removes them. [2](#0-1) 

`add_addr` always stores addresses with `last_connected_at_ms=0` and `attempts_count=0`. [3](#0-2) 

With those values, `is_connectable` returns `true`: the `attempts_count >= ADDR_MAX_RETRIES` branch (`0 >= 3`) is false, and the `attempts_count >= ADDR_MAX_FAILURES` branch (`0 >= 10`) is also false. [4](#0-3) 

`ADDR_MAX_RETRIES = 3` and `ADDR_MAX_FAILURES = 10` are confirmed constants. [5](#0-4) 

So phase 1 finds zero candidates. **Phase 2** groups addresses by `/16` network group (first two octets of the IPv4 address), sorts groups by size descending, takes the top `len/2` groups, and evicts 2 from each — **but only if `addrs.len() > 4`**: [6](#0-5) 

The `/16` grouping key is confirmed: [7](#0-6) 

When the attacker distributes exactly 4 addresses per /16 subnet across 4096 subnets (4096 × 4 = 16384), every group has `len == 4`. The condition `4 > 4` is `false`, so `flat_map` yields `None` for every group, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

The error is silently swallowed at `debug!` level in the discovery layer: [8](#0-7) 

Every subsequent `add_addr` call hits the same dead end. The store is locked at 16384 attacker-controlled addresses.

## Impact Explanation

Once locked, `fetch_addrs_to_attempt` requires `last_connected_at_ms` within the last 3 days; all attacker addresses have `last_connected_at_ms=0` and are excluded. `fetch_addrs_to_feeler` will return attacker addresses, the node will attempt feeler connections, fail, and increment `attempts_count`. After 3 failures (`ADDR_MAX_RETRIES`), those addresses become non-connectable and are evicted by phase 1 — but the attacker can continuously resend `Nodes` messages to refill the store faster than the node can exhaust them. During this sustained attack the node cannot record any honest peer addresses, cannot establish new outbound connections to honest peers, and loses the ability to sync with the honest chain tip. This constitutes a **High** impact: a vulnerability that could easily cause CKB network congestion / node isolation with low attacker cost.

## Likelihood Explanation

The attack requires only a single P2P connection to the victim node. The discovery protocol accepts `Nodes` messages from any connected peer with up to `MAX_ADDR_TO_SEND=1000` addresses per message. [9](#0-8) 

Filling 16384 slots requires approximately 17 rounds of `Nodes` messages. The attacker needs globally routable IPv4 addresses to pass `is_valid_addr`/`is_reachable`, but these can be real attacker-controlled IP space or addresses in attacker-controlled /16 blocks. The condition is deterministic and reproducible: any distribution of exactly 4 addresses per /16 subnet triggers it.

## Recommendation

Change the eviction threshold from `> 4` to `>= 2` (or `>= 1`) so any non-trivially-sized group is eligible for eviction:

```rust
// current (vulnerable):
if addrs.len() > 4 {

// fixed:
if addrs.len() >= 2 {
```

Additionally, add a hard fallback that always evicts at least one entry (e.g., the address with the oldest `last_tried_at_ms`) when `candidate_peers` would otherwise be empty and the store is full, guaranteeing `check_purge` never returns `EvictionFailed` when the store is full of connectable addresses. [10](#0-9) 

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
    assert!(result.is_err()); // EvictionFailed — store locked
    assert_eq!(peer_store.addr_manager().count(), 16384);
}
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
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

**File:** network/src/peer_store/types.rs (L63-76)
```rust
impl AddrInfo {
    /// Init
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
