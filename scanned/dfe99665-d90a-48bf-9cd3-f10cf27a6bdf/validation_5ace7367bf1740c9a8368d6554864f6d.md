### Title
Identify `Flags::DISCOVERY` Advertised Even When Discovery Protocol Is Disabled — (`util/launcher/src/lib.rs`)

---

### Summary

CKB's Identify protocol uses a `Flags` bitfield to advertise which protocols a node supports. In `util/launcher/src/lib.rs`, the flags value is initialized to `Flags::all()` and individual bits are removed when their corresponding protocols are disabled. However, `Flags::DISCOVERY` is **never removed** even when the Discovery protocol is absent from `support_protocols`. A node running without Discovery will falsely advertise `Flags::DISCOVERY` to every peer it connects to, causing those peers to store and propagate its address as a Discovery-capable node throughout the network.

---

### Finding Description

In `util/launcher/src/lib.rs`, the `start_network_and_rpc` function builds the `flags` value that is broadcast to all peers via the Identify protocol:

```rust
let mut flags = Flags::all();   // includes DISCOVERY, SYNC, RELAY, LIGHT_CLIENT, BLOCK_FILTER

if support_protocols.contains(&SupportProtocol::Relay) { ... }
else { flags.remove(Flags::RELAY); }          // ✓ correctly cleared

if support_protocols.contains(&SupportProtocol::Filter) { ... }
else { flags.remove(Flags::BLOCK_FILTER); }   // ✓ correctly cleared

if support_protocols.contains(&SupportProtocol::LightClient) { ... }
else { flags.remove(Flags::LIGHT_CLIENT); }   // ✓ correctly cleared

// Discovery protocol is conditionally registered in NetworkService::new()
// but Flags::DISCOVERY is NEVER removed here
``` [1](#0-0) 

The `Flags` enum defines `DISCOVERY = 0b10` as the bit that signals a node can serve peer address data: [2](#0-1) 

The Discovery protocol handler is only registered inside `NetworkService::new()` when `config.support_protocols.contains(&SupportProtocol::Discovery)`: [3](#0-2) 

The config explicitly documents Discovery as optional: [4](#0-3) 

When a peer connects and receives the Identify message, it stores the advertised flags directly into the peer store: [5](#0-4) 

Those stored flags are then used to filter which addresses are shared in Discovery `Nodes` responses: [6](#0-5) 

And to select which peers to dial for outbound connections: [7](#0-6) 

---

### Impact Explanation

A node with Discovery disabled but `Flags::DISCOVERY` set in its Identify message will:

1. Have its address stored in every connecting peer's peer store tagged as `Flags::DISCOVERY`-capable.
2. Be included in `Nodes` responses sent to any peer that issues a `GetNodes` with `required_flags` containing `Flags::DISCOVERY`.
3. Cause receiving nodes to dial it expecting Discovery service — the connection succeeds (Sync opens) but the Discovery protocol never opens, wasting a connection slot and yielding no peer address data.

This false capability advertisement propagates transitively: every node that receives the address via Discovery will re-share it to its own peers, spreading the false `Flags::DISCOVERY` tag network-wide. The result is systematic pollution of the peer store across the network with addresses that appear to serve Discovery but do not. Nodes seeking Discovery peers will repeatedly dial non-Discovery nodes, degrading their ability to build a healthy routing table and increasing susceptibility to eclipse attacks.

---

### Likelihood Explanation

The default `support_protocols` list includes Discovery, so the bug is dormant in default deployments. However, the configuration explicitly marks Discovery as optional and any operator who removes it from `support_protocols` (e.g., a light-client-only node, a relay-only node, or a node following the documented customization path) will silently trigger the mismatch. Because the false flag is encoded at startup and never updated, every session opened by or to that node carries the incorrect advertisement for the entire lifetime of the process. The propagation through Discovery means the impact scales with the number of peers the affected node reaches.

---

### Recommendation

Add the missing `else` branch for `Flags::DISCOVERY` in `util/launcher/src/lib.rs`, mirroring the pattern already used for `RELAY`, `BLOCK_FILTER`, and `LIGHT_CLIENT`:

```rust
if support_protocols.contains(&SupportProtocol::Discovery) {
    // Discovery is registered inside NetworkService::new()
} else {
    flags.remove(Flags::DISCOVERY);
}
``` [8](#0-7) 

---

### Proof of Concept

1. Configure a CKB node with `support_protocols = ["Ping", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert"]` (Discovery omitted).
2. Start the node. Observe that `NetworkService::new()` does **not** register a Discovery protocol handler (the `if config.support_protocols.contains(&SupportProtocol::Discovery)` branch is skipped).
3. Connect a second node. The second node receives an Identify message with `flags` containing `Flags::DISCOVERY` (bit `0b10` set), because `flags = Flags::all()` and `Flags::DISCOVERY` is never cleared.
4. The second node stores the first node's address with `Flags::DISCOVERY` set via `peer_store.add_outbound_addr(address, flags)`.
5. When a third node sends `GetNodes` with `required_flags = Flags::DISCOVERY`, the second node returns the first node's address.
6. The third node dials the first node; Sync opens but Discovery never opens, confirming the false advertisement.

The root cause is the missing `flags.remove(Flags::DISCOVERY)` branch at `util/launcher/src/lib.rs` line 429–476, while the analogous removals for `RELAY` (line 440), `BLOCK_FILTER` (line 455), and `LIGHT_CLIENT` (line 475) are all present. [1](#0-0)

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

**File:** network/src/protocols/identify/mod.rs (L421-423)
```rust
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
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

**File:** resource/ckb.toml (L111-112)
```text
# Supported protocols list, only "Sync" and "Identify" are mandatory, others are optional
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** network/src/peer_store/peer_store_impl.rs (L183-213)
```rust
    /// Get peers for outbound connection, this method randomly return recently connected peer addrs
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

**File:** network/src/peer_store/peer_store_impl.rs (L268-283)
```rust
    /// Return valid addrs that success connected, used for discovery.
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
