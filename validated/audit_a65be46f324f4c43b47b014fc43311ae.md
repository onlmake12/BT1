### Title
Inconsistent Address Normalization in `AddrManager` Dual-Mapping Creates Ghost Entries and Silently Bypasses Score-Based Peer Banning — (`network/src/peer_store/addr_manager.rs`)

---

### Summary

`AddrManager` stores peer data in two mappings (`addr_to_id` and `id_to_info`). The `add()` method inserts addresses **without normalization**, while `remove()`, `get()`, and `get_mut()` all normalize addresses through `base_addr()` before lookup. Any address containing a transport-layer protocol suffix (`/ws`, `/wss`, `/memory`, `/tls`) inserted via `add()` can never be found or removed afterward. This creates permanent ghost entries, silently fails score updates (bypassing score-based banning), and allows the same peer to accumulate duplicate entries under two different keys.

---

### Finding Description

`AddrManager` is a dual-mapping registry: [1](#0-0) 

```rust
pub struct AddrManager {
    next_id: u64,
    addr_to_id: HashMap<Multiaddr, u64>,   // key: raw Multiaddr
    id_to_info: HashMap<u64, AddrInfo>,    // key: numeric ID
    random_ids: Vec<u64>,
}
```

**`add()` stores the raw address with no normalization:** [2](#0-1) 

```rust
pub fn add(&mut self, mut addr_info: AddrInfo) {
    if let Some(&id) = self.addr_to_id.get(&addr_info.addr) { // raw addr
        ...
        return;
    }
    self.addr_to_id.insert(addr_info.addr.clone(), id);       // raw addr stored
    ...
}
```

**`remove()`, `get()`, and `get_mut()` all normalize via `base_addr()` first:** [3](#0-2) 

```rust
pub fn remove(&mut self, addr: &Multiaddr) -> Option<AddrInfo> {
    let base_addr = base_addr(addr);                          // normalized
    self.addr_to_id.remove(&base_addr).and_then(|id| { ... })
}
``` [4](#0-3) [5](#0-4) 

`base_addr()` strips transport-layer protocols: [6](#0-5) 

```rust
pub(crate) fn base_addr(addr: &Multiaddr) -> Multiaddr {
    addr.iter().filter_map(|p| {
        if matches!(p, Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_)) {
            None
        } else { Some(p) }
    }).collect()
}
```

**Concrete inconsistency:**

| Operation | Key used | Result for `/ip4/1.2.3.4/tcp/8114/ws` |
|---|---|---|
| `add()` | raw addr | stored as `/ip4/1.2.3.4/tcp/8114/ws` → ID 0 |
| `get()` / `get_mut()` | `base_addr()` → `/ip4/1.2.3.4/tcp/8114` | **NOT FOUND** |
| `remove()` | `base_addr()` → `/ip4/1.2.3.4/tcp/8114` | **NOT FOUND** |

**Duplicate-entry scenario (direct analog to BundlerRegistry):**

1. Peer advertises `/ip4/1.2.3.4/tcp/8114/ws` → `add()` stores it as ID 0.
2. Peer also advertises `/ip4/1.2.3.4/tcp/8114` → `add()` checks raw key, not found → stores as ID 1.
3. Two entries now exist for the same peer.
4. `ban_addr()` is called with `/ip4/1.2.3.4/tcp/8114/ws` → `remove()` normalizes to `/ip4/1.2.3.4/tcp/8114` → removes ID 1.
5. ID 0 (`/ip4/1.2.3.4/tcp/8114/ws`) remains as a ghost entry, permanently unfindable and unremovable. [7](#0-6) 

**Score-update bypass:**

`report()` calls `addr_manager.get_mut(addr)`, which normalizes. For any peer whose address was stored with a `/ws` suffix, `get_mut()` returns `None`, the score is never updated, and the peer is never banned via the score mechanism: [8](#0-7) 

WebSocket addresses reach `addr_manager` via `add_outbound_addr()`, which is called directly from the identify protocol handler with `context.session.address` (the live session address, which includes `/ws`): [9](#0-8) [10](#0-9) 

---

### Impact Explanation

1. **Score-based banning bypass**: Any peer connecting via WebSocket whose address is stored with `/ws` will never have its score updated by `report()`. Misbehaviors that should reduce score (and eventually trigger a ban) are silently ignored. The peer can misbehave indefinitely without being banned through the score mechanism.

2. **Ghost entries / removal failure**: `ban_addr()` calls `addr_manager.remove()`, which silently fails for `/ws` addresses. The peer's address remains in `addr_manager` and continues to be returned by `fetch_random()`, `fetch_addrs_to_attempt()`, and `fetch_addrs_to_feeler()`.

3. **Duplicate entries inflate count**: The same peer stored under two keys counts twice toward `ADDR_COUNT_LIMIT = 16384`, causing premature eviction of legitimate peers via `check_purge()`. [11](#0-10) 

---

### Likelihood Explanation

WebSocket transport is a first-class supported protocol in CKB's network stack (explicitly handled in `base_addr()`). Outbound peers' session addresses — which include `/ws` — are stored directly into `addr_manager` via `add_outbound_addr()` during the identify handshake. Any unprivileged peer that connects via WebSocket triggers this path automatically, with no special configuration required.

---

### Recommendation

Normalize the address in `add()` using `base_addr()` before inserting into `addr_to_id`, consistent with how `remove()`, `get()`, and `get_mut()` operate:

```rust
pub fn add(&mut self, mut addr_info: AddrInfo) {
    let normalized = base_addr(&addr_info.addr);
    addr_info.addr = normalized.clone();
    if let Some(&id) = self.addr_to_id.get(&normalized) {
        ...
        return;
    }
    self.addr_to_id.insert(normalized, id);
    ...
}
```

This ensures a single canonical key is used across all operations, eliminating ghost entries and the score-update bypass.

---

### Proof of Concept

```
1. Node A connects to victim CKB node via WebSocket.
   Session address: /ip4/A.A.A.A/tcp/8114/ws

2. Identify protocol fires → add_outbound_addr(/ip4/A.A.A.A/tcp/8114/ws) is called.
   addr_to_id stores: "/ip4/A.A.A.A/tcp/8114/ws" → ID 0  (raw, no normalization)

3. Node A sends malformed messages, triggering report(addr, Behaviour::UnexpectedMessage).
   report() calls addr_manager.get_mut("/ip4/A.A.A.A/tcp/8114/ws")
   → base_addr() strips /ws → looks up "/ip4/A.A.A.A/tcp/8114" → NOT FOUND → returns None.
   Score is never decremented. Peer is never banned via score mechanism.

4. ban_addr("/ip4/A.A.A.A/tcp/8114/ws") is called for a more serious offense.
   addr_manager.remove() normalizes → "/ip4/A.A.A.A/tcp/8114" → NOT FOUND.
   Ghost entry "/ip4/A.A.A.A/tcp/8114/ws" → ID 0 persists in addr_manager.
   fetch_random() continues returning this address to the dialer.
```

### Citations

**File:** network/src/peer_store/addr_manager.rs (L13-18)
```rust
pub struct AddrManager {
    next_id: u64,
    addr_to_id: HashMap<Multiaddr, u64>,
    id_to_info: HashMap<u64, AddrInfo>,
    random_ids: Vec<u64>,
}
```

**File:** network/src/peer_store/addr_manager.rs (L22-42)
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
        addr_info.random_id_pos = self.random_ids.len();
        self.id_to_info.insert(id, addr_info);
        self.random_ids.push(id);
        self.next_id += 1;
    }
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

**File:** network/src/peer_store/addr_manager.rs (L122-127)
```rust
    pub fn get(&self, addr: &Multiaddr) -> Option<&AddrInfo> {
        let base_addr = base_addr(addr);
        self.addr_to_id
            .get(&base_addr)
            .and_then(|id| self.id_to_info.get(id))
    }
```

**File:** network/src/peer_store/addr_manager.rs (L130-137)
```rust
    pub fn get_mut(&mut self, addr: &Multiaddr) -> Option<&mut AddrInfo> {
        let base_addr = base_addr(addr);
        if let Some(id) = self.addr_to_id.get(&base_addr) {
            self.id_to_info.get_mut(id)
        } else {
            None
        }
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

**File:** network/src/protocols/identify/mod.rs (L421-423)
```rust
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
```
