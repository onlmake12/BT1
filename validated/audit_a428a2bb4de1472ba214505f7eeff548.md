Looking at the actual code, I need to trace the full ban path for Onion3 addresses.

The code confirms the vulnerability. Here is the full analysis:

**`ban_addr`** (lines 286–292) uses `if let Some(...)` — when `multiaddr_to_socketaddr` returns `None` for an Onion3 address, the entire `ban_network` call is skipped, but `addr_manager.remove(addr)` still executes unconditionally. [1](#0-0) 

**`is_addr_banned`** in `ban_list.rs` also calls `multiaddr_to_socketaddr` and uses `.unwrap_or_default()` — returning `false` for any address where the conversion yields `None`, including all Onion3 addresses. [2](#0-1) 

**`addr_manager.fetch_random`** explicitly special-cases Onion3 addresses to include them in connection candidates even when `multiaddr_to_socketaddr` returns `None`, confirming Tor is a supported production transport. [3](#0-2) 

**`add_addr`** gates re-insertion on `is_addr_banned`, which always returns `false` for Onion3 — so after being removed from `addr_manager`, the peer can be re-added immediately via discovery. [4](#0-3) 

---

### Title
Onion3 (Tor) Peers Silently Bypass Score-Based Banning, Enabling Indefinite Reconnection — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`ban_addr` fails to insert Onion3 multiaddrs into the ban list because `multiaddr_to_socketaddr` returns `None` for them, causing the `if let Some(...)` guard to skip `ban_network`. The peer is removed from `addr_manager` but is never actually banned. Since `is_addr_banned` uses the same conversion and returns `false` for Onion3, the peer can reconnect and re-register itself without restriction.

### Finding Description
In `peer_store_impl.rs`, `ban_addr` is:

```rust
pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
    if let Some(addr) = multiaddr_to_socketaddr(addr) {   // None for Onion3 → skipped
        let network = ip_to_network(addr.ip());
        self.ban_network(network, timeout_ms, ban_reason)  // never called
    }
    self.addr_manager.remove(addr);                        // always called
}
```

For an Onion3 multiaddr, `multiaddr_to_socketaddr` returns `None`, so `ban_network` is never called and no entry is written to `ban_list`. The peer is evicted from `addr_manager` but the ban list remains empty for that peer.

`is_addr_banned` in `ban_list.rs` has the same blind spot:

```rust
pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
    multiaddr_to_socketaddr(addr)
        .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
        .unwrap_or_default()   // always false for Onion3
}
```

Because `add_addr` checks `is_addr_banned` before blocking re-insertion, a Tor peer that was "banned" can be re-added to `addr_manager` immediately via the discovery protocol.

### Impact Explanation
A misbehaving Tor peer can trigger the ban path repeatedly — getting removed from `addr_manager` each time — but is never actually prevented from reconnecting. The score-based misbehavior enforcement system is completely ineffective for the entire class of Onion3 peers. This allows a Tor peer to send invalid blocks, headers, or transactions, get "banned," reconnect, and repeat indefinitely with no cumulative penalty.

### Likelihood Explanation
Onion3 is explicitly supported: `addr_manager.fetch_random` contains a dedicated code path that includes Onion3 addresses in connection candidates even when `multiaddr_to_socketaddr` returns `None`. Any peer reachable over Tor that can trigger a negative score report (e.g., by sending invalid compact block data or a malformed header) can exploit this.

### Recommendation
`ban_addr` must handle non-IP transports explicitly. For Onion3 addresses, a separate ban store keyed on the onion hostname (or the full multiaddr prefix) should be maintained. `is_addr_banned` must be extended to check this store. Alternatively, if Tor support is not intended for production, Onion3 addresses should be rejected at the `add_addr` boundary.

### Proof of Concept
```rust
let mut peer_store = PeerStore::default();
// Construct a valid Onion3 multiaddr (56-char base32 host + port)
let onion_addr: Multiaddr =
    "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:1234"
        .parse().unwrap();
peer_store.add_addr(onion_addr.clone(), Flags::COMPATIBILITY).unwrap();

// Simulate score-based ban
peer_store.ban_addr(&onion_addr, 600_000, "misbehaviour".into());

// Ban list is empty — peer was never banned
assert_eq!(peer_store.ban_list().count(), 0);
// is_addr_banned returns false — peer can reconnect
assert!(!peer_store.is_addr_banned(&onion_addr));
// Peer can be re-added immediately
peer_store.add_addr(onion_addr.clone(), Flags::COMPATIBILITY).unwrap();
assert_eq!(peer_store.addr_manager().count(), 1); // back in the pool
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

**File:** network/src/peer_store/ban_list.rs (L68-72)
```rust
    pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
        multiaddr_to_socketaddr(addr)
            .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
            .unwrap_or_default()
    }
```

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
