### Title
`COMPATIBILITY` Flag Spoofing Bypasses Capability Check in `required_flags_filter`, Enabling Peer Store Pollution and Connection Slot Exhaustion — (File: network/src/peer_store/peer_store_impl.rs)

---

### Summary

The `required_flags_filter` function in `network/src/peer_store/peer_store_impl.rs` contains a special-case bypass: when the required capability set is exactly `RELAY | DISCOVERY | SYNC`, a peer advertising only the `COMPATIBILITY` flag (0b1) is unconditionally accepted as meeting that requirement. Any unprivileged inbound or outbound peer can exploit this by advertising `COMPATIBILITY` alone — without implementing sync, relay, or discovery — to pass capability gating, occupy peer slots, and inject non-functional addresses into the peer store that propagate network-wide via the discovery protocol.

---

### Finding Description

**Root cause — `required_flags_filter`:** [1](#0-0) 

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    if required == Flags::RELAY | Flags::DISCOVERY | Flags::SYNC {
        t.contains(required) || t.contains(Flags::COMPATIBILITY)
    } else {
        t.contains(required)
    }
}
```

The `COMPATIBILITY` flag is defined as bit 0b1 and documented as "Compatibility reserved": [2](#0-1) 

The default `required_flags` for a full node is `SYNC | DISCOVERY | RELAY`: [3](#0-2) 

This means the special-case branch fires for every standard full-node capability check. A peer sending `flag = 0x01` in its Identify message passes the filter identically to a peer that genuinely advertises all three protocol flags.

**Propagation path 1 — outbound connection (direct):**

When the local node connects outbound to a peer, `received_identify` is called. If `required_flags_filter` returns true, the local node opens all non-feeler protocols and stores the peer's address with the advertised flags: [4](#0-3) 

The address is stored via `add_outbound_addr` with `last_connected_at_ms = now`: [5](#0-4) 

**Propagation path 2 — inbound connection + feeler promotion:**

For inbound peers, `received_identify` stores the peer's flags in `identify_info` and then `process_listens` calls `add_remote_listen_addrs`, which reads those flags and stores the listen addresses in the peer store via `add_addr` (with `last_connected_at_ms = 0`): [6](#0-5) 

`fetch_addrs_to_feeler` returns addresses with `last_connected_at_ms = 0`, so the local node will probe the attacker's listen address via the feeler protocol. On that feeler connection, the attacker again sends `COMPATIBILITY`, which passes `required_flags_filter`, causing `add_outbound_addr` to store the address with `last_connected_at_ms = now` and `COMPATIBILITY` flag.

**Peer store broadcast:**

`fetch_random_addrs` uses `required_flags_filter` to select addresses for discovery broadcast: [7](#0-6) 

Because `COMPATIBILITY` passes the filter for `RELAY | DISCOVERY | SYNC`, the attacker's address is returned and broadcast to other nodes via the discovery protocol. Those nodes will also accept the address, attempt connections, and repeat the cycle. The address persists in the peer store for up to 7 days (`ADDR_TIMEOUT_MS`).

**Slot occupation window:**

The `ProtocolTypeCheckerService` disconnects peers that do not open all required protocols, but only after a 10-second timeout checked on a 30-second interval — up to a 40-second window per connection: [8](#0-7) 

---

### Impact Explanation

1. **Peer store pollution (network-wide):** Attacker addresses stored with `COMPATIBILITY` flag are treated as fully capable (`RELAY | DISCOVERY | SYNC`) by `fetch_random_addrs` and `fetch_addrs_to_attempt`, causing them to be broadcast via discovery and selected for outbound connection attempts across the network. These addresses persist for up to 7 days.

2. **Connection slot exhaustion:** An attacker can occupy inbound or outbound peer slots for up to 40 seconds per cycle before `ProtocolTypeCheckerService` disconnects them, then immediately reconnect. With multiple IPs, this can saturate the peer registry and prevent legitimate sync/relay peers from connecting.

3. **Sync and relay disruption:** If peer slots are filled with `COMPATIBILITY`-only peers that do not implement sync or relay, the node cannot download blocks or propagate transactions, degrading liveness.

---

### Likelihood Explanation

Any unprivileged peer reachable over TCP can connect to a CKB node, complete the Tentacle/SecIO handshake, open the Identify protocol (ID 2), and send a well-formed Identify message with `flag = 0x01`. No key material, operator access, or majority hashpower is required. The attack is fully automated and repeatable.

---

### Recommendation

1. **Remove the `COMPATIBILITY` bypass** from `required_flags_filter`. Peers that do not advertise the required flags should be disconnected at the Identify stage, not after a 40-second slot-occupation window.

2. **Do not store peer addresses in the peer store until the peer has actually opened the required protocols.** Currently, `add_outbound_addr` and `add_remote_listen_addrs` are called immediately upon receiving the Identify message, before protocol capability is verified.

3. **Deprecate the `COMPATIBILITY` flag** as a capability signal. If backward compatibility with old nodes is required, use a versioned handshake rather than a flag that bypasses capability filtering.

---

### Proof of Concept

```
1. Connect to a CKB mainnet node (TCP + SecIO handshake via Tentacle).
2. Open protocol ID 2 (Identify).
3. Send a packed Identify message:
     name          = "ckb"          // correct network name
     flag          = 0x0000000000000001  // COMPATIBILITY only
     client_version = "0.1.0"
     listen_addrs  = [attacker_ip:port]
4. Observe: the local node does NOT disconnect (required_flags_filter passes).
5. Observe: attacker_ip:port is stored in the peer store with flags=0x01.
6. Wait for the local node to broadcast attacker_ip:port to peers via discovery
   (fetch_random_addrs returns it because COMPATIBILITY passes the RELAY|DISCOVERY|SYNC filter).
7. Other nodes connect to attacker_ip:port, repeat from step 3.
8. Attacker's address propagates network-wide; nodes waste connection slots and
   sync resources on a peer that implements no useful protocol.
```

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L269-283)
```rust
    pub fn fetch_random_addrs(&mut self, count: usize, required_flags: Flags) -> Vec<AddrInfo> {
        // Get info:
        // 1. Connected within 7 days

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let addr_expired_ms = now_ms.saturating_sub(ADDR_TIMEOUT_MS);

        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };

        // get success connected addrs.
        self.addr_manager.fetch_random(count, filter)
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L407-413)
```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    if required == Flags::RELAY | Flags::DISCOVERY | Flags::SYNC {
        t.contains(required) || t.contains(Flags::COMPATIBILITY)
    } else {
        t.contains(required)
    }
}
```

**File:** network/src/protocols/identify/mod.rs (L415-443)
```rust
                if context.session.ty.is_outbound() {
                    // why don't set inbound here?
                    // because inbound address can't feeler during staying connected
                    // and if set it to peer store, it will be broadcast to the entire network,
                    // but this is an unverified address

                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });

                    if self.network_state.with_peer_registry_mut(|reg| {
                        reg.change_feeler_flags(&context.session.address, flags)
                    }) {
                        let _ = context
                            .open_protocols(
                                context.session.id,
                                TargetProtocol::Single(SupportProtocols::Feeler.protocol_id()),
                            )
                            .await;
                    } else if required_flags_filter(required_flags, flags) {
                        // The remote end can support all local protocols.
                        let _ = context
                            .open_protocols(
                                context.session.id,
                                TargetProtocol::Filter(Box::new(move |id| {
                                    id != &SupportProtocols::Feeler.protocol_id()
                                })),
                            )
                            .await;
```

**File:** network/src/protocols/identify/mod.rs (L472-494)
```rust
    fn add_remote_listen_addrs(&mut self, session: &SessionContext, addrs: Vec<Multiaddr>) {
        trace!(
            "IdentifyProtocol add remote listening addresses, session: {:?}, addresses : {:?}",
            session, addrs,
        );
        let flags = self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(session.id) {
                peer.listened_addrs = addrs.clone();
                peer.identify_info
                    .as_ref()
                    .map(|a| a.flags)
                    .unwrap_or(Flags::COMPATIBILITY)
            } else {
                Flags::COMPATIBILITY
            }
        });
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
```

**File:** network/src/protocols/identify/mod.rs (L564-580)
```rust
bitflags::bitflags! {
    /// Node Function Identification
    #[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
    pub struct Flags: u64 {
        /// Compatibility reserved
        const COMPATIBILITY = 0b1;
        /// Discovery protocol, which can provide peers data service
        const DISCOVERY = 0b10;
        /// Sync protocol can provide Block and Header download service
        const SYNC = 0b100;
        /// Relay protocol, which can provide CompactBlock and Transaction broadcast/forwarding services
        const RELAY = 0b1000;
        /// Light client protocol, which can provide Block / Transaction data and existence-proof services
        const LIGHT_CLIENT = 0b10000;
        /// Client-side block filter protocol can provide BlockFilter download service
        const BLOCK_FILTER = 0b100000;
    }
```

**File:** network/src/network.rs (L204-204)
```rust
            required_flags: Flags::SYNC | Flags::DISCOVERY | Flags::RELAY,
```

**File:** network/src/services/protocol_type_checker.rs (L23-24)
```rust
const TIMEOUT: Duration = Duration::from_secs(10);
const CHECK_INTERVAL: Duration = Duration::from_secs(30);
```
