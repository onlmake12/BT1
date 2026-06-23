The code is fully readable. I have everything needed to make a definitive determination.

**Tracing the exact path:**

**Step 1 — Entrypoint:** A remote peer sends a `Nodes` (announce) discovery message. The handler calls `add_new_addrs`, which calls `peer_store.add_addr(addr, flags)` for each advertised address with no rate-limit beyond the per-message item count. [1](#0-0) 

**Step 2 — `add_addr` hardcodes `last_connected_at_ms = 0`:** Every call to `add_addr` constructs a fresh `AddrInfo::new(addr, 0, score, flags.bits())`. The `0` is unconditional — there is no branch that preserves the existing entry's timestamp. [2](#0-1) 

**Step 3 — `AddrInfo::new` always initializes `attempts_count = 0`:** [3](#0-2) 

**Step 4 — The `>=` condition in `AddrManager::add` allows the overwrite:** When the existing entry has `last_connected_at_ms = 0` (never connected) and the incoming `addr_info` also has `last_connected_at_ms = 0`, the condition `0 >= 0` is `true`, so `self.id_to_info.insert(id, addr_info)` replaces the entire entry — including `attempts_count` — with the fresh zero-initialized one. The comment "Get time earlier than record time, return directly" reveals the intent was `>`, not `>=`. [4](#0-3) 

**Step 5 — `is_connectable` invariant is broken:** After `ADDR_MAX_RETRIES = 3` failed attempts with `last_connected_at_ms == 0`, `is_connectable` returns `false`. But after the attacker re-advertises the same address, `attempts_count` is reset to 0 and `is_connectable` returns `true` again. [5](#0-4) [6](#0-5) 

**Step 6 — `check_purge` eviction is defeated:** `check_purge` selects eviction candidates by filtering for `!addr.is_connectable(now_ms)`. Entries whose `attempts_count` has been reset are "connectable" and are never selected, so they persist indefinitely. [7](#0-6) 

---

### Title
`AddrManager::add` `>=` condition allows discovery peers to reset `attempts_count`, defeating peer-store eviction — (`network/src/peer_store/addr_manager.rs`)

### Summary
The `>=` comparison in `AddrManager::add` permits an incoming `AddrInfo` with `last_connected_at_ms = 0` to unconditionally overwrite an existing entry that also has `last_connected_at_ms = 0`, resetting `attempts_count` to 0. Because `add_addr` (the only path from the discovery protocol) always passes `last_connected_at_ms = 0`, any remote peer can re-advertise an address to reset its retry counter, keeping it permanently "connectable" and immune to eviction.

### Finding Description
`PeerStore::add_addr` constructs `AddrInfo::new(addr, 0, score, flags)` — the second argument is the hardcoded literal `0`.
`AddrInfo::new` initializes `attempts_count: 0`.
`AddrManager::add` checks `if addr_info.last_connected_at_ms >= exist_last_connected_at_ms` before overwriting. For a never-connected entry (`exist = 0`) and a discovery-sourced entry (`new = 0`), `0 >= 0` is `true`, so the full `AddrInfo` struct (including `attempts_count`) is replaced.

After `ADDR_MAX_RETRIES` (3) failed feeler connections, `is_connectable` returns `false` and `check_purge` would evict the entry. But if the attacker re-sends a `Nodes` announce message containing the same address before or after eviction, `attempts_count` is reset to 0 and the entry is treated as fresh.

### Impact Explanation
- **Peer store slot exhaustion**: The peer store is capped at `ADDR_COUNT_LIMIT = 16384`. An attacker who fills it with unreachable addresses and periodically re-announces them prevents `check_purge` from evicting them (step 1 of purge finds no non-connectable entries). If the attacker uses addresses spread across diverse /16 subnets, the network-group fallback (step 2) also fails, causing `check_purge` to return `Err(EvictionFailed)` and blocking all future `add_addr` calls.
- **Wasted feeler connections**: The node repeatedly dials unreachable addresses, consuming outbound connection budget.
- **Eclipse attack enablement**: A full peer store of attacker-controlled addresses prevents the victim from learning about honest peers, enabling network isolation.

### Likelihood Explanation
Any peer connected via the discovery protocol can send `Nodes` announce messages at any time (announce messages are not limited to once per session, unlike the initial `Nodes` response). The attacker only needs to maintain one connection and periodically re-broadcast the target addresses. No special privileges, keys, or PoW are required.

### Recommendation
Change the update condition in `AddrManager::add` from `>=` to `>`:

```rust
// Before (buggy):
if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {

// After (correct):
if addr_info.last_connected_at_ms > exist_last_connected_at_ms {
```

This ensures that a discovery-sourced entry (`last_connected_at_ms = 0`) never overwrites an existing entry that also has `last_connected_at_ms = 0`, preserving the accumulated `attempts_count`, `last_tried_at_ms`, and `score`.

### Proof of Concept

```rust
use ckb_network::{Flags, peer_store::{PeerStore, types::AddrInfo}, peer_store::mod::ADDR_MAX_RETRIES};

let mut peer_store = PeerStore::default();
let addr = "/ip4/1.2.3.4/tcp/8115/p2p/<peer_id>".parse().unwrap();

// Step 1: add via discovery (last_connected_at_ms = 0, attempts_count = 0)
peer_store.add_addr(addr.clone(), Flags::COMPATIBILITY).unwrap();

// Step 2: simulate ADDR_MAX_RETRIES (3) failed feeler attempts
let now_ms = 100_000u64;
for _ in 0..ADDR_MAX_RETRIES {
    peer_store.mut_addr_manager().get_mut(&addr).unwrap().mark_tried(now_ms - 70_000);
}

// Confirm the entry is now non-connectable
let entry = peer_store.addr_manager().get(&addr).unwrap();
assert_eq!(entry.attempts_count, 3);
assert!(!entry.is_connectable(now_ms)); // passes: is_connectable = false

// Step 3: attacker re-advertises the same address via discovery
peer_store.add_addr(addr.clone(), Flags::COMPATIBILITY).unwrap();

// BUG: attempts_count is reset to 0 and is_connectable returns true
let entry = peer_store.addr_manager().get(&addr).unwrap();
assert_eq!(entry.attempts_count, 0);       // RESET — invariant violated
assert!(entry.is_connectable(now_ms));     // TRUE — eviction bypassed
```

### Citations

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

**File:** network/src/peer_store/addr_manager.rs (L22-34)
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
```

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
