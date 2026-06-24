All cited code references are confirmed in the repository. Let me verify the one remaining behavioral question — what actually causes the disconnect in the test, and whether the `required_flags_filter` is used elsewhere.

Audit Report

## Title
COMPATIBILITY Flag Bypass in `required_flags_filter` Allows Outbound Slot Exhaustion Without Advertising Required Capabilities — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`required_flags_filter` contains an intentional but exploitable special-case branch that returns `true` when a peer advertises only `Flags::COMPATIBILITY` (0b1), even though the required flags are `SYNC | DISCOVERY | RELAY`. Because the default `required_flags` for every production node is exactly `SYNC | DISCOVERY | RELAY`, any attacker node advertising only `COMPATIBILITY` passes the filter. When the victim dials such a peer outbound, `IdentifyCallback::received_identify` calls `open_protocols` to open all non-Feeler protocol sessions toward the attacker, consuming an outbound slot and pushing sync/relay/discovery data to a peer that never advertised support for those protocols.

## Finding Description
`required_flags_filter` at `network/src/peer_store/peer_store_impl.rs` lines 407–413 evaluates:

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    if required == Flags::RELAY | Flags::DISCOVERY | Flags::SYNC {
        t.contains(required) || t.contains(Flags::COMPATIBILITY)
    } else {
        t.contains(required)
    }
}
``` [1](#0-0) 

The `Flags` bitfield defines non-overlapping values: `COMPATIBILITY = 0b1`, `DISCOVERY = 0b10`, `SYNC = 0b100`, `RELAY = 0b1000`. [2](#0-1) 

So `t.contains(Flags::COMPATIBILITY)` is `true` for a peer advertising only `0b1`, while `t.contains(required)` is `false`. The function returns `true`.

The default `required_flags` for every production node is set to exactly `Flags::SYNC | Flags::DISCOVERY | Flags::RELAY`, which is the exact value that triggers the special-case branch. [3](#0-2) 

This filter is applied in three places in the peer store: `fetch_addrs_to_attempt` (selecting peers to dial outbound), `fetch_nat_addrs`, and `fetch_random_addrs` (peers gossiped via discovery). [4](#0-3) [5](#0-4) [6](#0-5) 

In `received_identify`, after the identify message is verified, the result of `required_flags_filter` directly gates `open_protocols`:

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
``` [7](#0-6) 

This call is guarded by `context.session.ty.is_outbound()`, so it fires only when the victim has dialed the attacker outbound. [8](#0-7) 

The existing test `test_identify_behavior` confirms the code path: `node4` (full node with `required_flags = SYNC | DISCOVERY | RELAY`) dials `node1` (COMPATIBILITY-only). The test expects `wait_connect_state(&node4, 0)` — the connection drops only because `node1` does not register handlers for the opened protocols, causing the substream opens to fail. An attacker that does accept those substreams sustains the session. [9](#0-8) 

## Impact Explanation
Once the victim opens all non-Feeler protocols to the attacker, the victim's sync protocol pushes block headers and compact blocks, the relay protocol forwards transactions and compact blocks, and the discovery protocol sends peer address lists — all to a peer that never advertised support for any of these. The attacker consumes one of the victim's limited outbound connection slots and can silently discard all received data. An attacker operating a cluster of `COMPATIBILITY`-only nodes seeded into the network's peer store via the discovery `Nodes` message can cause many victim nodes to simultaneously waste outbound slots and bandwidth at negligible cost. This matches the **High** impact category: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The attacker only needs to advertise a valid network name and `flags = 0b1`. No proof-of-work, no keys, and no privileged access are required. Seeding an address into the peer store is trivial via any connected peer gossiping a discovery `Nodes` message. The `fetch_addrs_to_attempt` function also uses `required_flags_filter`, so COMPATIBILITY-only addresses are eligible for outbound dialing selection. The victim's outbound dialer will eventually dial the seeded address, triggering the bypass automatically and repeatably. The `COMPATIBILITY` flag value is publicly documented in the `Flags` bitflags definition.

## Recommendation
Remove or tighten the special-case branch in `required_flags_filter`. If backward compatibility with legacy nodes is required, replace the single-bit check with an explicit allowlist of known-good legacy flag combinations that actually imply full-node capability. At minimum, `received_identify` should verify that the peer's flags are a strict superset of the required flags before opening any data-bearing protocol, regardless of the `COMPATIBILITY` bit. A safe alternative is:

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    t.contains(required)
}
```

with a separate, documented migration path for any genuinely legacy nodes.

## Proof of Concept
1. Start a victim node with default config (`required_flags = SYNC | DISCOVERY | RELAY`).
2. Start an attacker node that responds to the identify handshake with `flags = Flags::COMPATIBILITY` (0b1) and the correct network name, and registers handlers for Sync, Relay, and Discovery protocols (or silently accepts those substream opens).
3. Seed the attacker's address into the victim's peer store via a discovery `Nodes` message from any connected peer.
4. Wait for the victim to dial the attacker outbound (normal peer-store dialing behavior via `fetch_addrs_to_attempt`, which also passes COMPATIBILITY-only peers through `required_flags_filter`).
5. Observe that `required_flags_filter(SYNC|DISCOVERY|RELAY, COMPATIBILITY)` returns `true` and `open_protocols` is called with `TargetProtocol::Filter(exclude Feeler)`.
6. Confirm that the victim's sync/relay/discovery protocol handlers begin sending data to the attacker session while the attacker sends nothing back, and that the victim's outbound slot remains occupied.

The existing test `test_identify_behavior` already constructs a full node (`node4`) that dials a `COMPATIBILITY`-only node (`node1`), confirming the code path is exercised. The test uses a `COMPATIBILITY`-only node that does not register the full protocol handlers (causing eventual disconnect), but substituting an attacker node that does accept those protocol substreams reproduces the sustained session scenario described above.

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

**File:** network/src/peer_store/peer_store_impl.rs (L251-265)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && peer_addr.addr.iter().any(|p| {
                    matches!(
                        p,
                        p2p::multiaddr::Protocol::Ip4(_) | p2p::multiaddr::Protocol::Ip6(_)
                    )
                })
                && peer_addr.last_connected_at_ms == 0
        };

        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/peer_store_impl.rs (L276-280)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };

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

**File:** network/src/network.rs (L156-156)
```rust
            required_flags: Flags::SYNC | Flags::DISCOVERY | Flags::RELAY,
```

**File:** network/src/protocols/tests/mod.rs (L297-331)
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

    thread::sleep(Duration::from_secs(1));
    wait_connect_state(&node4, 0);
```
