### Title
Missing `Flags::DISCOVERY` Removal When Discovery Protocol Is Disabled - (`util/launcher/src/lib.rs`)

### Summary

In `start_network_and_rpc`, the node initializes its capability flags with `Flags::all()` and then selectively removes flags for disabled protocols. However, `Flags::DISCOVERY` is never removed even when `SupportProtocol::Discovery` is absent from the configured `support_protocols`. This causes the node to falsely advertise Discovery capability in its Identify handshake, misleading peers into treating it as a valid discovery source when the Discovery protocol handler is not running.

### Finding Description

In `util/launcher/src/lib.rs`, the `start_network_and_rpc` function builds the node's capability `Flags` bitmask: [1](#0-0) 

It starts with `Flags::all()` and then conditionally removes flags for each optional protocol that is not configured: [2](#0-1) 

The pattern is consistent for `Flags::RELAY`, `Flags::BLOCK_FILTER`, and `Flags::LIGHT_CLIENT` — each is removed when its corresponding `SupportProtocol` variant is absent. However, there is **no corresponding removal of `Flags::DISCOVERY`** when `SupportProtocol::Discovery` is absent from `support_protocols`.

The `Flags` bitmask is defined as: [3](#0-2) 

`Flags::DISCOVERY` (`0b10`) is a distinct, meaningful capability bit. The `SupportProtocol::Discovery` variant is explicitly optional in the config: [4](#0-3) 

The resulting `flags` value is passed directly into the `NetworkService` constructor as the node's identify announcement: [5](#0-4) 

This flags value is then encoded into the Identify protocol message broadcast to all connecting peers via `Identify::new`: [6](#0-5) 

### Impact Explanation

Peers receive the Identify message and store the node's address with the falsely-set `Flags::DISCOVERY` bit in the peer store: [7](#0-6) 

The `required_flags_filter` function uses these stored flags to select peers for outbound connections: [8](#0-7) 

Peers seeking discovery sources (`fetch_random_addrs`, `fetch_addrs_to_attempt`, `fetch_nat_addrs`) will include this node in their candidate sets and attempt to open the Discovery protocol with it. Since the Discovery handler is not running on the node, the protocol open will fail or be silently ignored, wasting outbound connection slots and degrading peer discovery quality across the network. The peer store becomes polluted with addresses tagged with incorrect capability flags, causing persistent misdirection of connection attempts.

**Impact: Medium** — Network topology degradation; nodes advertising false Discovery capability cause peers to waste connection attempts and degrade the quality of peer discovery, potentially slowing IBD and peer propagation for nodes relying on discovery.

### Likelihood Explanation

**Likelihood: Medium** — The default config enables all protocols including Discovery, so this only manifests when an operator explicitly removes `Discovery` from `support_protocols`. The config file documents this as a valid option: [9](#0-8) 

Nodes running in specialized roles (e.g., pure sync nodes, light client servers) may reasonably disable Discovery. The bug is silent — no error is logged, and the node continues operating normally while broadcasting incorrect capability.

### Recommendation

Add the missing `flags.remove(Flags::DISCOVERY)` branch in `start_network_and_rpc`, analogous to the existing removals for `RELAY`, `BLOCK_FILTER`, and `LIGHT_CLIENT`:

```diff
+        if !support_protocols.contains(&SupportProtocol::Discovery) {
+            flags.remove(Flags::DISCOVERY);
+        }
+
         if support_protocols.contains(&SupportProtocol::Relay) {
```

### Proof of Concept

1. Configure a CKB node with `support_protocols = ["Ping", "Identify", "Sync", "Relay", "Time", "Alert"]` (omitting `"Discovery"`).
2. Start the node. The Discovery protocol handler is not registered.
3. Connect a peer and observe the Identify message. The `flag` field will contain `Flags::DISCOVERY` (`0b10`) set, because `Flags::all()` is used and never cleared for Discovery.
4. The peer stores this node's address with `Flags::DISCOVERY` set in its peer store.
5. When the peer calls `fetch_random_addrs` with `required_flags = Flags::DISCOVERY`, this node's address is returned and a connection attempt is made.
6. The peer attempts to open protocol ID `1` (`/ckb/discovery`) — the node has no handler registered for it, so the protocol open silently fails.
7. The peer wastes a connection slot and receives no discovery data, while the node's address remains incorrectly flagged in the peer store indefinitely.

### Citations

**File:** util/launcher/src/lib.rs (L428-429)
```rust
        let support_protocols = &self.args.config.network.support_protocols;
        let mut flags = Flags::all();
```

**File:** util/launcher/src/lib.rs (L431-476)
```rust
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

**File:** util/launcher/src/lib.rs (L497-508)
```rust
        let network_controller = NetworkService::new(
            Arc::clone(&network_state),
            protocols,
            required_protocol_ids,
            (
                shared.consensus().identify_name(),
                self.version.to_string(),
                flags,
            ),
            TransportType::Tcp,
        )
        .start(shared.async_handle())
```

**File:** network/src/protocols/identify/mod.rs (L420-423)
```rust

                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
```

**File:** network/src/protocols/identify/mod.rs (L524-534)
```rust
impl Identify {
    fn new(name: String, flags: Flags, client_version: String) -> Self {
        Identify {
            encode_data: packed::Identify::new_builder()
                .name(name.as_str())
                .flag(flags.bits())
                .client_version(client_version.as_str())
                .build()
                .as_bytes(),
            name,
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

**File:** util/app-config/src/configs/network.rs (L218-251)
```rust
#[derive(Clone, Debug, Copy, Eq, PartialEq, Serialize, Deserialize, Hash)]
#[allow(missing_docs)]
pub enum SupportProtocol {
    Ping,
    Discovery,
    Identify,
    Feeler,
    DisconnectMessage,
    Sync,
    Relay,
    Time,
    Alert,
    LightClient,
    Filter,
    HolePunching,
}

#[allow(missing_docs)]
pub fn default_support_all_protocols() -> Vec<SupportProtocol> {
    vec![
        SupportProtocol::Ping,
        SupportProtocol::Discovery,
        SupportProtocol::Identify,
        SupportProtocol::Feeler,
        SupportProtocol::DisconnectMessage,
        SupportProtocol::Sync,
        SupportProtocol::Relay,
        SupportProtocol::Time,
        SupportProtocol::Alert,
        SupportProtocol::LightClient,
        SupportProtocol::Filter,
        SupportProtocol::HolePunching,
    ]
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

**File:** resource/ckb.toml (L111-112)
```text
# Supported protocols list, only "Sync" and "Identify" are mandatory, others are optional
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```
