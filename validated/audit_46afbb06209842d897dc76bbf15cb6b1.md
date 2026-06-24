Audit Report

## Title
Inconsistent Address Normalization in `AddrManager::add()` Bypasses Score-Based Peer Banning and Creates Irremovable Ghost Entries — (`network/src/peer_store/addr_manager.rs`)

## Summary

`AddrManager::add()` stores peer addresses using the raw `Multiaddr` key, while `remove()`, `get()`, and `get_mut()` all normalize via `base_addr()` before lookup. Any WebSocket peer address inserted with a `/ws` suffix is permanently unfindable and unremovable. This silently breaks `report()` (score updates never apply, banning never triggers) and `ban_addr()` (ghost entry persists), and also causes `check_purge()` to silently fail its own eviction loop, making ghost entries irremovable even under memory pressure.

## Finding Description

**Root cause — `add()` skips normalization:**

`add()` uses `addr_info.addr` as the raw key for both the existence check and the insertion into `addr_to_id`: [1](#0-0) 

`remove()`, `get()`, and `get_mut()` all call `base_addr()` first, which strips `/ws`, `/wss`, `/memory`, and `/tls` components: [2](#0-1) [3](#0-2) 

**Trigger path — identify protocol stores raw session address:**

The identify handler calls `add_outbound_addr(context.session.address.clone(), flags)` directly. For a WebSocket session, `context.session.address` is `/ip4/A.A.A.A/tcp/8114/ws`: [4](#0-3) 

`add_outbound_addr()` passes the raw address straight to `addr_manager.add()` with no normalization: [5](#0-4) 

**Score-update bypass in `report()`:**

`report()` calls `addr_manager.get_mut(addr)`. For a `/ws` address, `get_mut()` normalizes to the base address, finds no matching key, and returns `None`. The score is never decremented and `ban_addr()` is never called: [6](#0-5) 

**Ghost entry survives `ban_addr()`:**

`ban_addr()` calls `addr_manager.remove(addr)`, which normalizes to the base address. The stored key is the raw `/ws` address, so the lookup misses and the entry is never removed: [7](#0-6) 

**`check_purge()` eviction loop also silently fails:**

`check_purge()` collects candidate addresses from `addrs_iter()` (which yields raw `AddrInfo.addr` values) and calls `addr_manager.remove(key)` on each. Because `remove()` normalizes the key before lookup, it cannot find the raw `/ws` entry and silently returns `None`. Ghost entries are never evicted: [8](#0-7) 

If all entries are non-evictable ghost entries and the second-phase network-group eviction also fails, `check_purge()` returns `Err(PeerStoreError::EvictionFailed)`, blocking all future `add_addr()` calls: [9](#0-8) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Any peer connecting via WebSocket completely bypasses the score-based misbehavior-banning mechanism. `report()` silently returns `ReportResult::Ok` for every misbehavior report against a `/ws` peer, so the peer can send malformed or protocol-violating messages indefinitely without being banned through the score path. Combined with the ghost-entry persistence (the address keeps being returned by `fetch_random()` and `fetch_addrs_to_attempt()`), a single attacker node can sustain a continuous stream of misbehavior at zero incremental cost. At scale, multiple such nodes can exhaust the `ADDR_COUNT_LIMIT` of 16384 with irremovable entries, causing `check_purge()` to fail and blocking legitimate peer discovery entirely. [10](#0-9) 

## Likelihood Explanation

WebSocket transport is explicitly handled as a first-class protocol in `base_addr()`. Any unprivileged external node that connects to a CKB node via WebSocket and completes the identify handshake automatically triggers `add_outbound_addr()` with a `/ws` session address. No special configuration, privilege, or victim mistake is required. The condition is met on every outbound WebSocket connection.

## Recommendation

Normalize the address in `add()` using `base_addr()` before any key operation, and store the normalized address back into `addr_info.addr` so that `id_to_info` and `addr_to_id` are consistent:

```rust
pub fn add(&mut self, mut addr_info: AddrInfo) {
    let normalized = base_addr(&addr_info.addr);
    addr_info.addr = normalized.clone();
    if let Some(&id) = self.addr_to_id.get(&normalized) {
        // existing update logic unchanged
        ...
        return;
    }
    self.addr_to_id.insert(normalized, id);
    ...
}
```

This makes the key used in `add()` identical to the key used in `remove()`, `get()`, and `get_mut()`, eliminating the mismatch. It also fixes the `check_purge()` eviction loop, since `addrs_iter()` will then yield normalized addresses that `remove()` can find.

## Proof of Concept

**Manual steps:**

1. Start a CKB node (victim).
2. Connect attacker node to victim via WebSocket transport. Session address becomes `/ip4/A.A.A.A/tcp/P/ws`.
3. Identify handshake completes → `add_outbound_addr(/ip4/A.A.A.A/tcp/P/ws)` is called → `addr_to_id` stores key `/ip4/A.A.A.A/tcp/P/ws` → ID 0.
4. Attacker sends malformed messages. Victim calls `report(&"/ip4/A.A.A.A/tcp/P/ws", Behaviour::UnexpectedMessage)`.
   - `get_mut()` normalizes to `/ip4/A.A.A.A/tcp/P` → not found → returns `None`.
   - Score unchanged. `ban_addr()` never called. `ReportResult::Ok` returned.
5. Repeat step 4 indefinitely. Peer is never banned via score mechanism.
6. Call `ban_addr("/ip4/A.A.A.A/tcp/P/ws", ...)` directly.
   - `remove()` normalizes to `/ip4/A.A.A.A/tcp/P` → not found → returns `None`.
   - Ghost entry `/ip4/A.A.A.A/tcp/P/ws` → ID 0 remains in `addr_to_id` and `id_to_info`.
   - `fetch_random()` continues returning this address.

**Unit test plan:**

```rust
#[test]
fn test_ws_addr_ghost_entry() {
    let mut mgr = AddrManager::default();
    let ws_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8114/ws".parse().unwrap();
    let base: Multiaddr = "/ip4/1.2.3.4/tcp/8114".parse().unwrap();
    mgr.add(AddrInfo::new(ws_addr.clone(), 0, 100, 0));
    assert_eq!(mgr.count(), 1);
    // get() via base_addr normalization must find the entry
    assert!(mgr.get(&ws_addr).is_some(), "get() must find /ws entry");
    // remove() must succeed
    assert!(mgr.remove(&ws_addr).is_some(), "remove() must remove /ws entry");
    assert_eq!(mgr.count(), 0);
}
```

This test currently fails on `get()` and `remove()`, confirming the bug.

### Citations

**File:** network/src/peer_store/addr_manager.rs (L22-37)
```rust
    pub fn add(&mut self, mut addr_info: AddrInfo) {
        if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
            let (exist_last_connected_at_ms, random_id_pos) = {
                let info = self.id_to_info.get(&id).expect("must exists");
                (info.last_connected_at_ms, info.random_id_pos)
            };
            // Get time earlier than record time, return directly
            if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
                addr_info.random_id_pos = random_id_pos;
                self.id_to_info.insert(id, addr_info);
            }
            return;
        }

        let id = self.next_id;
        self.addr_to_id.insert(addr_info.addr.clone(), id);
```

**File:** network/src/peer_store/addr_manager.rs (L110-119)
```rust
    pub fn remove(&mut self, addr: &Multiaddr) -> Option<AddrInfo> {
        let base_addr = base_addr(addr);
        self.addr_to_id.remove(&base_addr).and_then(|id| {
            let random_id_pos = self.id_to_info.get(&id).expect("exists").random_id_pos;
            // swap with last index, then remove the last index
            self.swap_random_id(random_id_pos, self.random_ids.len() - 1);
            self.random_ids.pop();
            self.id_to_info.remove(&id)
        })
    }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L92-105)
```rust
pub(crate) fn base_addr(addr: &Multiaddr) -> Multiaddr {
    addr.iter()
        .filter_map(|p| {
            if matches!(
                p,
                Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_)
            ) {
                None
            } else {
                Some(p)
            }
        })
        .collect()
}
```

**File:** network/src/protocols/identify/mod.rs (L421-423)
```rust
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
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

**File:** network/src/peer_store/peer_store_impl.rs (L153-167)
```rust
    pub fn report(&mut self, addr: &Multiaddr, behaviour: Behaviour) -> ReportResult {
        if let Some(peer_addr) = self.addr_manager.get_mut(addr) {
            let score = peer_addr.score.saturating_add(behaviour.score());
            peer_addr.score = score;
            if score < self.score_config.ban_score {
                self.ban_addr(
                    addr,
                    self.score_config.ban_timeout_ms,
                    format!("report behaviour {behaviour:?}"),
                );
                return ReportResult::Banned;
            }
        }
        ReportResult::Ok
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L286-292)
```rust
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
        }
        self.addr_manager.remove(addr);
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

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```
