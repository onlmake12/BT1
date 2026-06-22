### Title
COMPATIBILITY Flag Bypass in `required_flags_filter` Grants Full Protocol Access Without Advertising SYNC/DISCOVERY/RELAY — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

A special-case branch in `required_flags_filter` allows any peer advertising only the `COMPATIBILITY` (0b1) flag to pass the flag check that is supposed to require `SYNC | DISCOVERY | RELAY`. When the victim node dials such a peer outbound, `IdentifyCallback::received_identify` opens all non-Feeler protocols to that peer, giving it full sync/relay/discovery session access despite never advertising support for those protocols.

---

### Finding Description

`required_flags_filter` in `network/src/peer_store/peer_store_impl.rs` contains an explicit special case:

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    if required == Flags::RELAY | Flags::DISCOVERY | Flags::SYNC {
        t.contains(required) || t.contains(Flags::COMPATIBILITY)
    } else {
        t.contains(required)
    }
}
``` [1](#0-0) 

When `required` equals the default `SYNC | DISCOVERY | RELAY`, the function returns `true` if the peer's flags `t` contain only `COMPATIBILITY` (0b1), without containing any of SYNC, DISCOVERY, or RELAY.

The default `required_flags` for every production node is:

```rust
required_flags: Flags::SYNC | Flags::DISCOVERY | Flags::RELAY,
``` [2](#0-1) 

In `IdentifyCallback::received_identify`, after the identify message is verified, the result of `required_flags_filter` directly gates opening all non-Feeler protocols:

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
``` [3](#0-2) 

The comment "The remote end can support all local protocols" is factually wrong when the peer only carries `COMPATIBILITY`. The victim proceeds to open Sync, Relay, Discovery, Ping, Time, Alert, etc. sessions toward the attacker.

**Constraint:** the `open_protocols` call is guarded by `if context.session.ty.is_outbound()`, so it only fires when the victim has dialed the attacker outbound. [4](#0-3) 

---

### Impact Explanation

Once the victim opens all non-Feeler protocols to the attacker:

- The victim's sync protocol pushes block headers and compact blocks to the attacker.
- The victim's relay protocol forwards transactions and compact blocks.
- The victim's discovery protocol sends peer address lists.
- The attacker consumes one of the victim's limited outbound connection slots.
- The attacker can silently drop all received data (zero reciprocation), wasting victim CPU, memory, and bandwidth.

An attacker operating a cluster of `COMPATIBILITY`-only nodes that have been seeded into the network's peer store (via the discovery protocol) can cause many victim nodes to simultaneously waste outbound slots and bandwidth, contributing to network-wide congestion at very low cost to the attacker.

---

### Likelihood Explanation

- The attacker only needs to advertise a valid network name and `flags = 0b1` (COMPATIBILITY). No PoW, no keys, no privileged access required.
- Getting an address into the peer store is trivial: any connected peer can gossip attacker addresses via the discovery `Nodes` message.
- The victim's outbound dialer will eventually dial the seeded address, triggering the bypass automatically.
- The `COMPATIBILITY` flag value (0b1) is publicly documented in the `Flags` bitflags definition. [5](#0-4) 

---

### Recommendation

The special-case branch in `required_flags_filter` should be removed or tightened. If backward compatibility with legacy nodes is required, the accepted legacy flag set should be an explicit allowlist of known-good legacy flag combinations that actually imply full-node capability, not a single bit that any attacker can trivially set. Alternatively, `received_identify` should verify that the peer's flags are a superset of the required flags before opening any data-bearing protocol, regardless of the `COMPATIBILITY` bit.

---

### Proof of Concept

1. Start a victim node with default config (`required_flags = SYNC | DISCOVERY | RELAY`).
2. Start an attacker node that sends an identify message with `flags = Flags::COMPATIBILITY` (0b1) and the correct network name.
3. Seed the attacker's address into the victim's peer store via a discovery `Nodes` message from any connected peer.
4. Wait for the victim to dial the attacker outbound.
5. Observe that `required_flags_filter(SYNC|DISCOVERY|RELAY, COMPATIBILITY)` returns `true` and `open_protocols` is called with `TargetProtocol::Filter(exclude Feeler)`.
6. Confirm that the victim's sync/relay/discovery protocol handlers begin sending data to the attacker session, while the attacker sends nothing back.

The existing test in `network/src/protocols/tests/mod.rs` already constructs nodes with `Flags::COMPATIBILITY` as both advertised and required flags, confirming the code path is exercised and the bypass is reachable. [6](#0-5)

### Citations

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

**File:** network/src/network.rs (L156-156)
```rust
            required_flags: Flags::SYNC | Flags::DISCOVERY | Flags::RELAY,
```

**File:** network/src/protocols/identify/mod.rs (L415-415)
```rust
                if context.session.ty.is_outbound() {
```

**File:** network/src/protocols/identify/mod.rs (L434-443)
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

**File:** network/src/protocols/tests/mod.rs (L297-329)
```rust
#[test]
fn test_identify_behavior() {
    let node1 = net_service_start(
        "/test/1".to_string(),
        false,
        Flags::COMPATIBILITY,
        Flags::COMPATIBILITY,
    );
    let node2 = net_service_start(
        "/test/2".to_string(),
        false,
        Flags::COMPATIBILITY,
        Flags::COMPATIBILITY,
    );
    let node3 = net_service_start(
        "/test/1".to_string(),
        false,
        Flags::COMPATIBILITY,
        Flags::COMPATIBILITY,
    );

    let node4 = net_service_start(
        "/test/1".to_string(),
        false,
        Flags::SYNC | Flags::RELAY | Flags::DISCOVERY | Flags::BLOCK_FILTER,
        Flags::SYNC | Flags::RELAY | Flags::DISCOVERY,
    );

    node4.dial(
        &node1,
        TargetProtocol::Single(SupportProtocols::Identify.protocol_id()),
    );

```
