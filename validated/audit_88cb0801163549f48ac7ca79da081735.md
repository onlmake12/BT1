### Title
Onion3 Peers Cannot Be Effectively Banned — Ban State Never Stored for Non-IP Addresses (`network/src/peer_store/ban_list.rs`, `network/src/peer_store/peer_store_impl.rs`)

---

### Summary

The `BanList` stores bans exclusively as `IpNetwork` entries. When `ban_addr` is called for an Onion3 multiaddr, `multiaddr_to_socketaddr` returns `None`, so no ban entry is ever written. Subsequent calls to `is_addr_banned` for the same Onion3 addr also return `None` → `false` via `unwrap_or_default`, allowing the address to be immediately re-admitted to the addr store.

---

### Finding Description

**`ban_addr` silently skips the ban for Onion3 addresses:** [1](#0-0) 

`multiaddr_to_socketaddr` returns `None` for Onion3 multiadds. The `if let Some(addr)` branch is skipped entirely — no `BannedAddr` is inserted into `ban_list`. Only `addr_manager.remove(addr)` executes, which merely evicts the address from the current in-memory store.

**`is_addr_banned` always returns `false` for Onion3:** [2](#0-1) 

`unwrap_or_default()` on `Option<bool>` returns `false` when `multiaddr_to_socketaddr` yields `None`. The `BanList` inner map is `HashMap<IpNetwork, BannedAddr>` — there is no mechanism to store or check non-IP bans. [3](#0-2) 

**`add_addr` and `add_outbound_addr` rely entirely on `is_addr_banned`:** [4](#0-3) [5](#0-4) 

Since `is_addr_banned` returns `false`, the Onion3 addr passes the guard and is re-inserted into `addr_manager`.

**Onion3 is a first-class, production-supported address type in CKB:**

The identify protocol explicitly propagates Onion3 listen addresses to peers: [6](#0-5) 

`addr_manager.fetch_random` gives Onion3 addresses special treatment, returning them for dialing even when `is_connectable` is false: [7](#0-6) 

The node has a configurable `onion_server` option confirming Tor/Onion3 is an intended production transport: [8](#0-7) 

---

### Impact Explanation

A misbehaving Onion3 peer that triggers `report()` → `ban_addr()` is never actually banned. Its address is removed from the addr store but can be immediately re-relayed by any peer via the discovery protocol, causing it to be re-added. The node will continue to dial and interact with the misbehaving peer indefinitely. The ban invariant — that a peer whose score drops below `ban_score` must not be re-admissible — is broken for all non-IP address types. **Scoped impact: Medium (2001–10000).**

---

### Likelihood Explanation

Any operator running CKB with Tor enabled (via `onion_server` config) is affected. The attack requires only that a misbehaving Onion3 peer exist and that any connected peer relay its address via discovery — both are normal P2P operations requiring no privilege.

---

### Recommendation

`BanList` must be extended to store non-IP bans. One approach: add a `HashSet<Multiaddr>` (or a keyed structure on the base addr) alongside `HashMap<IpNetwork, BannedAddr>`. `ban_addr` should insert the raw Onion3 multiaddr into this set when `multiaddr_to_socketaddr` returns `None`. `is_addr_banned` should check this set before returning `false`. The same ban expiry logic (`ban_until`) should apply.

---

### Proof of Concept

```rust
// Construct an Onion3 multiaddr
let onion3_addr: Multiaddr = "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:1234"
    .parse().unwrap();

let mut peer_store = PeerStore::default();

// Step 1: add the Onion3 addr
peer_store.add_addr(onion3_addr.clone(), Flags::SYNC).unwrap();
assert!(peer_store.addr_manager().get(&onion3_addr).is_some());

// Step 2: ban it (simulating report() → ban_addr())
peer_store.ban_addr(&onion3_addr, 86_400_000, "misbehavior".into());

// addr_manager entry is removed
assert!(peer_store.addr_manager().get(&onion3_addr).is_none());

// Step 3: is_addr_banned returns false — no ban was stored
assert!(!peer_store.is_addr_banned(&onion3_addr)); // BUG: should be true

// Step 4: re-add succeeds — ban is bypassed
peer_store.add_addr(onion3_addr.clone(), Flags::SYNC).unwrap();
assert!(peer_store.addr_manager().get(&onion3_addr).is_some()); // BUG: should be rejected
```

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

**File:** network/src/peer_store/ban_list.rs (L13-16)
```rust
pub struct BanList {
    inner: HashMap<IpNetwork, BannedAddr>,
    insert_count: usize,
}
```

**File:** network/src/peer_store/ban_list.rs (L68-72)
```rust
    pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
        multiaddr_to_socketaddr(addr)
            .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
            .unwrap_or_default()
    }
```

**File:** network/src/protocols/identify/mod.rs (L217-224)
```rust
                .filter(|addr| {
                    if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
                        !self.global_ip_only || is_reachable(socket_addr.ip())
                    } else {
                        // allow /onion3 address
                        addr.iter()
                            .any(|protocol| matches!(protocol, Protocol::Onion3(_)))
                    }
```

**File:** network/src/peer_store/addr_manager.rs (L74-91)
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
            }
```

**File:** network/src/network.rs (L1046-1048)
```rust
                let proxy_config_enable =
                    config.proxy.proxy_url.is_some() || config.onion.onion_server.is_some();

```
