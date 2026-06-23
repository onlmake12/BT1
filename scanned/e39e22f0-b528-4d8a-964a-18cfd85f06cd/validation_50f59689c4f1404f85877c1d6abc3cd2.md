### Title
Peer Flag Filter OR Bypass Allows COMPATIBILITY-Only Peers to Satisfy RELAY|DISCOVERY|SYNC Requirements — (File: `network/src/peer_store/peer_store_impl.rs`)

### Summary

The `required_flags_filter` function in CKB's peer store uses a logical OR condition that allows a peer advertising only the `COMPATIBILITY` flag (value `0b1`) to pass a filter that requires `RELAY | DISCOVERY | SYNC`. This mirrors the Solidity report's pattern exactly: an OR guard intended for a special case (backward compatibility) undermines the primary restriction (protocol capability enforcement). Any unprivileged network peer can exploit this by advertising only `COMPATIBILITY` in its identify message, causing honest nodes to select it for outbound connections and share its address via discovery even though it supports none of the required protocols.

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

When `required` equals the full set `RELAY | DISCOVERY | SYNC`, the function returns `true` if the peer has **either** all three flags **or** only the `COMPATIBILITY` flag (`0b1`). A peer with `flags = 0b1` (COMPATIBILITY only) satisfies the second branch of the OR, bypassing the requirement for RELAY, DISCOVERY, and SYNC entirely.

**Flag definitions:** [2](#0-1) 

`COMPATIBILITY = 0b1`, `DISCOVERY = 0b10`, `SYNC = 0b100`, `RELAY = 0b1000`. A peer advertising only `0b1` does not support any of the three required protocols.

**Call site 1 — outbound peer selection:** [3](#0-2) 

`fetch_addrs_to_attempt` calls `required_flags_filter(required_flags, ...)` to select peers for outbound connections. A COMPATIBILITY-only peer passes this filter when `required_flags = RELAY | DISCOVERY | SYNC`.

**Call site 2 — discovery address sharing:** [4](#0-3) 

`fetch_random_addrs` uses the same filter. When a remote peer sends `GetNodes` with `required_flags = RELAY | DISCOVERY | SYNC`, the node returns COMPATIBILITY-only peers as valid candidates, spreading them across the network.

**Call site 3 — identify protocol handler:** [5](#0-4) 

After the identify handshake, `required_flags_filter(required_flags, flags)` is called again with the flags received from the peer. A peer sending `flags = COMPATIBILITY` passes this check, causing the node to open all non-Feeler protocols (SYNC, RELAY, DISCOVERY) with a peer that does not support them.

**Attack flow:**
1. Attacker runs a modified CKB node that sends `flag = 0b1` (COMPATIBILITY only) in its identify message.
2. The attacker's node gets discovered and stored in honest nodes' peer stores with `flags = COMPATIBILITY`.
3. When an honest node calls `fetch_addrs_to_attempt` with `required_flags = RELAY | DISCOVERY | SYNC`, the attacker's address passes `required_flags_filter` and is selected for an outbound connection.
4. After the TCP connection is established and identify runs, `required_flags_filter(RELAY|DISCOVERY|SYNC, COMPATIBILITY)` returns `true` again, so the honest node opens SYNC, RELAY, and DISCOVERY protocols toward the attacker.
5. The attacker's node does not respond to any protocol messages. The connection slot is occupied but yields no useful data.
6. When honest nodes request addresses via discovery (`GetNodes` with `required_flags = RELAY|DISCOVERY|SYNC`), the attacker's address is included in responses, propagating the pollution to other nodes.

### Impact Explanation

An unprivileged attacker running one or more nodes advertising only `COMPATIBILITY` can:
- **Waste outbound connection slots** on honest nodes: each slot occupied by a COMPATIBILITY-only peer is unavailable for legitimate sync/relay peers.
- **Pollute peer stores network-wide** via discovery: honest nodes share the attacker's address as a valid RELAY|DISCOVERY|SYNC peer, causing other nodes to also waste connection slots.
- **Degrade sync and relay performance**: if enough connection slots are occupied by useless peers, block propagation and transaction relay slow down.
- At scale (multiple attacker nodes), this contributes to an eclipse attack by crowding out legitimate peers from connection tables.

Severity: **Medium** — no direct fund loss, but measurable degradation of P2P connectivity and potential contribution to eclipse conditions.

### Likelihood Explanation

The attack requires only running a modified CKB node (or any TCP server) that sends a crafted identify message with `flag = 0b1`. No privileged access, no keys, no majority hashpower. The attacker's node can be discovered organically or by connecting to honest nodes directly. The bypass is deterministic and requires no brute force. A single attacker node wastes at least one connection slot per victim; a handful of attacker nodes can meaningfully degrade connectivity.

### Recommendation

Remove the COMPATIBILITY bypass from `required_flags_filter` when used for peer selection, or enforce it only during the initial connection phase (before identify) and then re-check strict flags after the identify handshake:

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    // Remove the COMPATIBILITY bypass; require exact flag satisfaction.
    t.contains(required)
}
```

If backward compatibility with genuinely old nodes is required, track whether a peer has completed the identify handshake and apply the COMPATIBILITY bypass only for pre-identify connections, then enforce strict flags post-identify. Alternatively, deprecate the COMPATIBILITY bypass entirely and require all peers to advertise explicit protocol flags.

### Proof of Concept

1. Run a TCP server that speaks the CKB p2p handshake and sends an identify message with `flag = 1` (COMPATIBILITY only, no RELAY/DISCOVERY/SYNC bits set).
2. Connect this server to an honest CKB full node so it gets added to the peer store with `flags = 0b1`.
3. Observe that `required_flags_filter(Flags::RELAY | Flags::DISCOVERY | Flags::SYNC, Flags::COMPATIBILITY)` evaluates to `true` (the OR branch at line 409 fires).
4. Observe that `fetch_addrs_to_attempt` returns the attacker's address when selecting outbound peers.
5. Observe that the honest node opens a connection and attempts to open SYNC/RELAY/DISCOVERY protocols toward the attacker.
6. Observe that the attacker's address is included in `GetNodes` responses sent to third-party nodes, confirming network-wide propagation.

The deterministic evaluation of `required_flags_filter` at: [6](#0-5) 

confirms the bypass without any runtime uncertainty.

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L201-212)
```rust
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L276-282)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };

        // get success connected addrs.
        self.addr_manager.fetch_random(count, filter)
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

**File:** network/src/protocols/identify/mod.rs (L434-450)
```rust
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
                    } else {
                        // The remote end cannot support all local protocols.
                        warn!(
                            "Session closed from IdentifyProtocol due to peer's flag not meeting the requirements"
                        );
                        return MisbehaveResult::Disconnect;
                    }
```

**File:** network/src/protocols/identify/mod.rs (L564-581)
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
}
```
