Audit Report

## Title
`Flags::DISCOVERY` Never Cleared When Discovery Protocol Is Disabled — (`util/launcher/src/lib.rs`)

## Summary
In `util/launcher/src/lib.rs`, `flags` is initialized to `Flags::all()` and individual bits are removed when their corresponding protocols are absent from `support_protocols`. However, `Flags::DISCOVERY` is never removed, even though the Discovery protocol handler is conditionally registered in `NetworkService::new()`. Any node running without Discovery will falsely advertise `Flags::DISCOVERY` in every Identify message, causing peers to store and propagate its address as Discovery-capable throughout the network's peer store.

## Finding Description
In `util/launcher/src/lib.rs` at line 429, `flags` is set to `Flags::all()`. The code then removes `Flags::RELAY` (line 440), `Flags::BLOCK_FILTER` (line 455), and `Flags::LIGHT_CLIENT` (line 475) when those protocols are absent. There is no corresponding branch for `SupportProtocol::Discovery` — confirmed by the absence of any `SupportProtocol::Discovery` reference in `lib.rs`.

Meanwhile, in `network/src/network.rs` lines 896–914, the Discovery protocol handler is only registered when `config.support_protocols.contains(&SupportProtocol::Discovery)`. The two code paths are inconsistent: the handler registration is conditional, but the flag advertisement is not.

When a peer connects and the Identify exchange completes, the remote node's flags are stored directly into the peer store via `peer_store.add_outbound_addr(context.session.address.clone(), flags)` at `network/src/protocols/identify/mod.rs` line 422. Those stored flags are then used to filter candidates in `fetch_addrs_to_attempt` (line 208) and `fetch_random_addrs` (line 277) — both of which apply `required_flags_filter` against the stored `peer_addr.flags`. A Discovery-disabled node stored with `Flags::DISCOVERY` set will pass these filters and be returned to callers seeking Discovery-capable peers.

## Impact Explanation
This is a concrete case of **suboptimal/incorrect implementation of CKB's peer state storage mechanism** (peer store). The peer store accumulates incorrect capability metadata for any Discovery-disabled node. Nodes seeking Discovery peers will repeatedly dial addresses that pass the `Flags::DISCOVERY` filter but never open the Discovery protocol, wasting outbound connection slots and degrading the quality of the routing table. The false advertisement propagates transitively via Discovery `Nodes` responses, polluting peer stores across the network. This maps to **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**, as the peer store — a core state storage component — is systematically populated with incorrect flags data.

## Likelihood Explanation
The default `support_protocols` list includes Discovery, so the bug is dormant in default deployments. It is triggered by any operator who removes Discovery from `support_protocols`, which the configuration explicitly documents as optional (`resource/ckb.toml` line 111: "only 'Sync' and 'Identify' are mandatory, others are optional"). Light-client-only nodes or relay-only nodes following the documented customization path will silently trigger the mismatch. The false flag is set at startup and never corrected for the lifetime of the process.

## Recommendation
Add the missing `else` branch for `Flags::DISCOVERY` in `util/launcher/src/lib.rs`, immediately after the existing pattern for `RELAY`, `BLOCK_FILTER`, and `LIGHT_CLIENT`:

```rust
if support_protocols.contains(&SupportProtocol::Discovery) {
    // Discovery is registered inside NetworkService::new()
} else {
    flags.remove(Flags::DISCOVERY);
}
```

## Proof of Concept
1. Configure a CKB node with `support_protocols` omitting `"Discovery"`.
2. Start the node. Confirm `NetworkService::new()` skips the Discovery handler registration (the `if config.support_protocols.contains(&SupportProtocol::Discovery)` branch at `network/src/network.rs:897` is not taken).
3. Connect a second node. Capture the Identify message received by the second node; verify bit `0b10` (`Flags::DISCOVERY`) is set in the advertised flags, because `flags = Flags::all()` and no `flags.remove(Flags::DISCOVERY)` is ever executed.
4. Inspect the second node's peer store; confirm the first node's address is stored with `Flags::DISCOVERY` set.
5. Issue a `GetNodes` request with `required_flags = Flags::DISCOVERY` from a third node to the second node; confirm the first node's address is returned.
6. Dial the first node from the third node; confirm Sync opens but Discovery never opens, proving the false advertisement. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** util/launcher/src/lib.rs (L428-476)
```rust
        let support_protocols = &self.args.config.network.support_protocols;
        let mut flags = Flags::all();

        if support_protocols.contains(&SupportProtocol::Relay) {
            let relayer_v3 = Relayer::new(chain_controller.clone(), Arc::clone(&sync_shared));

            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::RelayV3,
                Box::new(relayer_v3),
                Arc::clone(&network_state),
            ));
        } else {
            flags.remove(Flags::RELAY);
        }

        if support_protocols.contains(&SupportProtocol::Filter) {
            let filter = BlockFilter::new(Arc::clone(&sync_shared));

            protocols.push(
                CKBProtocol::new_with_support_protocol(
                    SupportProtocols::Filter,
                    Box::new(filter),
                    Arc::clone(&network_state),
                )
                .compress(false),
            );
        } else {
            flags.remove(Flags::BLOCK_FILTER);
        }

        if support_protocols.contains(&SupportProtocol::Time) {
            let net_timer = NetTimeProtocol::default();
            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::Time,
                Box::new(net_timer),
                Arc::clone(&network_state),
            ));
        }

        if support_protocols.contains(&SupportProtocol::LightClient) {
            let light_client = LightClientProtocol::new(shared.clone());
            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::LightClient,
                Box::new(light_client),
                Arc::clone(&network_state),
            ));
        } else {
            flags.remove(Flags::LIGHT_CLIENT);
        }
```

**File:** network/src/network.rs (L896-914)
```rust
        // Discovery protocol
        if config
            .support_protocols
            .contains(&SupportProtocol::Discovery)
        {
            let addr_mgr = DiscoveryAddressManager {
                network_state: Arc::clone(&network_state),
                discovery_local_address: config.discovery_local_address,
            };
            let disc_meta = SupportProtocols::Discovery.build_meta_with_service_handle(move || {
                ProtocolHandle::Callback(Box::new(DiscoveryProtocol::new(
                    addr_mgr,
                    config
                        .discovery_announce_check_interval_secs
                        .map(Duration::from_secs),
                )))
            });
            protocol_metas.push(disc_meta);
        }
```

**File:** network/src/protocols/identify/mod.rs (L421-423)
```rust
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
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

**File:** network/src/peer_store/peer_store_impl.rs (L201-209)
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L276-279)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };
```

**File:** resource/ckb.toml (L111-112)
```text
# Supported protocols list, only "Sync" and "Identify" are mandatory, others are optional
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```
