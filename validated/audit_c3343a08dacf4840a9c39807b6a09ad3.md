### Title
Peer Capability Flags Silently Overwritten via Discovery Protocol `Nodes` Message — (`network/src/peer_store/addr_manager.rs`)

---

### Summary

`AddrManager::add` uses a `>=` comparison on `last_connected_at_ms` to decide whether to overwrite an existing peer entry. Because `PeerStore::add_addr` always supplies `last_connected_at_ms = 0`, any call to `add_addr` for an address that was previously discovered (and therefore also has `last_connected_at_ms = 0`) unconditionally overwrites the stored `AddrInfo`, including its `flags`, `score`, `attempts_count`, and `last_tried_at_ms`. Any connected peer can exploit this through the discovery protocol's `Nodes` message to silently downgrade the capability flags of legitimate peers in the victim node's peer store, degrading or blocking its ability to find sync and relay peers.

---

### Finding Description

In `network/src/peer_store/addr_manager.rs`, `AddrManager::add` checks whether to overwrite an existing entry:

```rust
// Get time earlier than record time, return directly
if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
    addr_info.random_id_pos = random_id_pos;
    self.id_to_info.insert(id, addr_info);
}
```

The intent is: "only update if the incoming record is at least as recent." However, `PeerStore::add_addr` — the function called for every address received from the discovery protocol — always constructs `AddrInfo` with `last_connected_at_ms = 0`:

```rust
pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
    ...
    self.addr_manager
        .add(AddrInfo::new(addr, 0, score, flags.bits()));
    Ok(())
}
```

Discovered-but-never-connected peers also have `last_connected_at_ms = 0` in the store. Therefore the condition `0 >= 0` evaluates to `true`, and the entire `AddrInfo` — including `flags`, `score`, `attempts_count`, and `last_tried_at_ms` — is replaced with the attacker-supplied values.

The discovery protocol handler `DiscoveryAddressManager::add_new_addrs` feeds attacker-controlled `flags` directly from the wire into `add_addr`:

```rust
fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
    for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
        self.network_state.with_peer_store_mut(|peer_store| {
            if let Err(err) = peer_store.add_addr(addr.clone(), flags) { ... }
        });
    }
}
```

The `Nodes` wire message carries a per-node `flags` field (`packed::Node2`) that is fully attacker-controlled. There is no validation that the flags in a `Nodes` response are consistent with what the target address actually advertised.

---

### Impact Explanation

Peer selection for outbound sync and relay connections filters by capability flags:

- `fetch_addrs_to_attempt` applies `required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))`.
- `fetch_random_addrs` (used for discovery gossip) applies the same filter.
- `fetch_nat_addrs` (hole-punching) also filters by `required_flags`.

By sending a `Nodes` message that lists known peer addresses with `flags = Flags::COMPATIBILITY` (the minimum, value `0b1`), an attacker strips `SYNC`, `RELAY`, `DISCOVERY`, `LIGHT_CLIENT`, and `BLOCK_FILTER` bits from those entries. The victim node will subsequently exclude those peers from all sync and relay candidate pools. If the attacker covers all entries in the peer store that have `last_connected_at_ms = 0` (i.e., all discovered-but-not-yet-connected peers), the node cannot bootstrap new outbound sync or relay connections, effectively isolating it from the network for those service types.

Additionally, the overwrite resets `attempts_count` and `last_tried_at_ms` to `0`, erasing the node's memory of failed connection attempts and causing it to re-attempt connections to unreachable addresses, wasting outbound connection slots.

---

### Likelihood Explanation

Any peer that has established a session with the victim node can send `Nodes` discovery messages. No special role, key, or privilege is required. The attacker only needs to know the multiaddrs of peers already in the victim's peer store — information that is itself gossiped via the same discovery protocol. The attack is repeatable: the attacker can re-send the downgraded entries whenever the victim re-discovers them with correct flags.

---

### Recommendation

Change the comparison in `AddrManager::add` from `>=` to `>`:

```rust
// Only overwrite if the incoming record is strictly newer
if addr_info.last_connected_at_ms > exist_last_connected_at_ms {
    addr_info.random_id_pos = random_id_pos;
    self.id_to_info.insert(id, addr_info);
}
```

This ensures that `add_addr` (which always passes `last_connected_at_ms = 0`) can never overwrite an existing entry, regardless of its stored timestamp. Separately, when updating an existing entry, consider merging flags with bitwise OR rather than replacing them, so that capability bits can only be added, not removed, by untrusted discovery messages.

---

### Proof of Concept

1. Victim node V discovers peer P via the discovery protocol; `add_addr(P.addr, SYNC | RELAY | DISCOVERY)` is called, storing `AddrInfo { flags: 0b1110, last_connected_at_ms: 0, ... }`.
2. Attacker A connects to V (any inbound or outbound session).
3. A sends a `DiscoveryMessage::Nodes` with one item: `Node { addresses: [P.addr], flags: COMPATIBILITY (0b1) }`.
4. V's `received` handler calls `add_new_addrs(session_id, [(P.addr, COMPATIBILITY)])`.
5. `add_addr(P.addr, COMPATIBILITY)` is called → `AddrManager::add` with `last_connected_at_ms = 0`.
6. Existing entry for P has `last_connected_at_ms = 0`; condition `0 >= 0` is `true`; entry is overwritten with `flags = 0b1`.
7. V now calls `fetch_addrs_to_attempt(n, SYNC, ...)`: `required_flags_filter(SYNC, COMPATIBILITY)` returns `false` for P; P is excluded.
8. V cannot find P as a sync candidate despite P being a valid sync peer.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L184-213)
```rust
    pub fn fetch_addrs_to_attempt<F>(
        &mut self,
        count: usize,
        required_flags: Flags,
        filter: F,
    ) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
        // Get info:
        // 1. Not already connected
        // 2. Connected within 3 days

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let peers = &self.connected_peers;
        let addr_expired_ms = now_ms.saturating_sub(ADDR_TRY_TIMEOUT_MS);

        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && peer_addr
                    .connected(|t| t > addr_expired_ms && t <= now_ms.saturating_sub(DIAL_INTERVAL))
                && required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
        };

        // get addrs that can attempt.
        self.addr_manager.fetch_random(count, filter)
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

**File:** network/src/peer_store/types.rs (L39-76)
```rust
/// Address info
#[derive(Debug, Clone, Hash, Eq, PartialEq, Serialize, Deserialize)]
pub struct AddrInfo {
    /// Multiaddr
    pub addr: Multiaddr,
    /// Score about this addr
    pub score: Score,
    /// Last connected time
    pub last_connected_at_ms: u64,
    /// Last try time
    pub last_tried_at_ms: u64,
    /// Attempts count
    pub attempts_count: u32,
    /// Random id
    pub random_id_pos: usize,
    /// Flags
    #[serde(default = "default_flags")]
    pub flags: u64,
}

fn default_flags() -> u64 {
    Flags::COMPATIBILITY.bits()
}

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
