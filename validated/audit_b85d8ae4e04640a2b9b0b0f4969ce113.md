### Title
Onion3 `is_connectable` Bypass in `fetch_random` Allows Permanent Pollution of Feeler Connection Pool — (`network/src/peer_store/addr_manager.rs`)

### Summary

The `fetch_random` function in `AddrManager` unconditionally returns Onion3 addresses regardless of their `is_connectable` state. An unprivileged remote peer can inject Onion3 multiaddrs via the discovery protocol, which pass ingress validation, get stored, and are repeatedly returned to the feeler dialer even after `attempts_count` reaches `ADDR_MAX_RETRIES` (3). Because `mark_tried` is called before each dial attempt, these addresses accumulate failed attempts but are never suppressed, permanently consuming feeler connection slots.

---

### Finding Description

**Root cause — `fetch_random` Onion3 branch bypasses `is_connectable`:** [1](#0-0) 

For addresses where `multiaddr_to_socketaddr` returns `None` (all Onion3 addresses), the code evaluates:

```rust
if addr_info.is_connectable(now_ms)
    || addr_info.addr.iter().any(|p| matches!(p, Protocol::Onion3(_)))
```

The `||` short-circuits: any Onion3 address is pushed to results regardless of `is_connectable`.

**`is_connectable` permanently returns `false` after 3 failed attempts with no prior connection:** [2](#0-1) 

`ADDR_MAX_RETRIES = 3`: [3](#0-2) 

**Onion3 addresses pass discovery ingress validation:** [4](#0-3) 

`multiaddr_to_socketaddr` returns `None` for Onion3, so the `None => true` branch passes every Onion3 address through `is_valid_addr`.

**`mark_tried` is called unconditionally before each dial attempt:** [5](#0-4) 

`mark_tried` is called for every address returned by `fetch_addrs_to_feeler`, including Onion3, before the actual dial. For `TransportType::Tcp`, the feeler filter is `|_| true`, so Onion3 addresses are not filtered out at the dial stage either. [6](#0-5) 

**Concrete call sequence:**

1. Attacker connects to victim node (standard P2P)
2. Attacker sends discovery `GetNodes`/`Nodes` messages containing N Onion3 multiaddrs
3. `add_new_addrs` → `is_valid_addr` returns `true` (Onion3 → `None` branch) → `add_addr` stores each with `attempts_count=0`, `last_connected_at_ms=0` [7](#0-6) 

4. `OutboundPeerService::dial_feeler` runs on each tick → `fetch_addrs_to_feeler(10, |_|true)` → `fetch_random` returns Onion3 addresses (Onion3 bypass fires) → `mark_tried` increments `attempts_count` for each
5. After 3 ticks: `attempts_count >= ADDR_MAX_RETRIES`, `last_connected_at_ms == 0` → `is_connectable = false`
6. On the next tick: `fetch_random` still returns them (Onion3 bypass), `mark_tried` is called again (no-op on the counter since it saturates), feeler slots consumed

**Partial mitigations that do NOT fully prevent the issue:**

- `tried_in_last_minute`: suppresses re-fetch for 60 seconds per address, but after that window the address is returned again indefinitely
- `check_purge`: evicts non-connectable addresses only when the store reaches `ADDR_COUNT_LIMIT = 16384`; the attacker can continuously inject fresh Onion3 addresses to replace evicted ones [8](#0-7) 

---

### Impact Explanation

The feeler dialer is capped at `FEELER_CONNECTION_COUNT = 10` addresses per interval. [9](#0-8) 

An attacker injecting enough Onion3 addresses can saturate all 10 feeler slots with permanently dead addresses, preventing the node from probing new reachable peers. This degrades peer discovery and outbound connection quality over time. It does not crash the node or compromise consensus, placing it squarely in the medium-impact range (suboptimal state, degraded connectivity).

---

### Likelihood Explanation

- Requires only a single P2P connection to the victim — no special privileges
- Discovery protocol accepts Onion3 addresses without any IP-reachability check
- The bypass is unconditional in production code; no configuration disables it
- `mark_tried` is called before the dial, so the counter increments even when the transport cannot reach Onion3 (no Tor configured)

---

### Recommendation

In the `None` branch of `fetch_random`, apply `is_connectable` uniformly before the Onion3 check, or add a separate guard that only bypasses `is_connectable` for Onion3 addresses that have **never been attempted** (`attempts_count == 0`):

```rust
None => {
    if filter(&addr_info) {
        let is_onion3 = addr_info.addr.iter().any(|p| matches!(p, Protocol::Onion3(_)));
        // Allow Onion3 only if not yet exhausted
        if addr_info.is_connectable(now_ms)
            || (is_onion3 && addr_info.attempts_count < ADDR_MAX_RETRIES)
        {
            addr_infos.push(addr_info);
        }
    }
}
```

Additionally, consider filtering Onion3 addresses at ingress in `is_valid_addr` when the node has no Tor transport configured.

---

### Proof of Concept

```rust
// Unit test sketch (production AddrManager, no mocks)
use ckb_network::peer_store::addr_manager::AddrManager;
use ckb_network::peer_store::types::AddrInfo;
use p2p::multiaddr::Multiaddr;

let mut mgr = AddrManager::default();
let onion3_addr: Multiaddr =
    "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:1234"
        .parse().unwrap();

let mut info = AddrInfo::new(onion3_addr, 0, 100, 1);
// Simulate ADDR_MAX_RETRIES (3) failed attempts with no successful connection
info.attempts_count = 3; // >= ADDR_MAX_RETRIES
// last_connected_at_ms stays 0

mgr.add(info);

let now_ms = ckb_systemtime::unix_time_as_millis();
// is_connectable must return false
let stored = mgr.addrs_iter().next().unwrap();
assert!(!stored.is_connectable(now_ms), "address should be non-connectable");

// fetch_random should return 0 results — but currently returns 1
let results = mgr.fetch_random(10, |_| true);
assert_eq!(results.len(), 0, "non-connectable Onion3 must not be returned");
// This assertion FAILS on current code, confirming the bypass
```

### Citations

**File:** network/src/peer_store/addr_manager.rs (L74-90)
```rust
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
                        } else {
                            debug!(
                                "addr {:?} is not connectable and not an onion address",
                                addr_info.addr
                            );
                        }
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

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
```

**File:** network/src/services/outbound_peer.rs (L22-22)
```rust
const FEELER_CONNECTION_COUNT: usize = 10;
```

**File:** network/src/services/outbound_peer.rs (L56-68)
```rust
    fn dial_feeler(&mut self) {
        let now_ms = unix_time_as_millis();
        let filter = |peer_addr: &AddrInfo| match self.transport_type {
            TransportType::Tcp => true,
            TransportType::Ws => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_) | Protocol::Tcp(_))),
            TransportType::Wss => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_))),
        };
```

**File:** network/src/services/outbound_peer.rs (L69-78)
```rust
        let attempt_peers = self.network_state.with_peer_store_mut(|peer_store| {
            let paddrs = peer_store.fetch_addrs_to_feeler(FEELER_CONNECTION_COUNT, filter);
            for paddr in paddrs.iter() {
                // mark addr as tried
                if let Some(paddr) = peer_store.mut_addr_manager().get_mut(&paddr.addr) {
                    paddr.mark_tried(now_ms);
                }
            }
            paddrs
        });
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

**File:** network/src/peer_store/peer_store_impl.rs (L327-355)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }

        // Evicting invalid data in the peer store is a relatively rare operation
        // There are certain cleanup strategies here:
        // 1. First evict the nodes that have reached the eviction condition
        // 2. If the first step is unsuccessful, enter the network segment grouping mode
        //  2.1. Group current data according to network segment
        //  2.2. Sort according to the amount of data in the same network segment
        //  2.3. In the network segment with more than 4 peer, randomly evict 2 peer

        let now_ms = ckb_systemtime::unix_time_as_millis();
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
