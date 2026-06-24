The code confirms the claim. Let me verify one more detail about how flags flow through the identify protocol.

Audit Report

## Title
Missing `Flags::DISCOVERY` Removal When Discovery Protocol Is Disabled — (`util/launcher/src/lib.rs`)

## Summary
In `util/launcher/src/lib.rs`, the `flags` bitfield is initialized to `Flags::all()` and individual bits are removed when their corresponding protocols are absent from `support_protocols`. However, `Flags::DISCOVERY` is never removed, even when Discovery is not registered. Any node running without Discovery will falsely advertise `Flags::DISCOVERY` to every peer via the Identify protocol, causing those peers to store and propagate its address as Discovery-capable throughout the network.

## Finding Description
In `util/launcher/src/lib.rs` at line 429, `flags` is set to `Flags::all()`. The code then conditionally removes `Flags::RELAY` (line 440), `Flags::BLOCK_FILTER` (line 455), and `Flags::LIGHT_CLIENT` (line 475) when their protocols are absent. There is no analogous branch for `Flags::DISCOVERY`. Meanwhile, in `network/src/network.rs` lines 896–914, the Discovery protocol handler is only registered when `config.support_protocols.contains(&SupportProtocol::Discovery)`. The mismatch means a node without Discovery still broadcasts `Flags::DISCOVERY` (bit `0b10`) in its Identify message. When a peer receives this message, `received_identify` in `network/src/protocols/identify/mod.rs` line 422 calls `peer_store.add_outbound_addr(address, flags)`, storing the false flag. `fetch_addrs_to_attempt` (line 208) and `fetch_random_addrs` (line 277) in `peer_store_impl.rs` both filter by `required_flags_filter`, so the falsely-tagged address will be returned to any caller requesting `Flags::DISCOVERY`-capable peers. Existing guards (the `required_flags_filter` check) are not insufficient per se — they are the mechanism that propagates the incorrect data, not a defense against it.

## Impact Explanation
This is a correctness defect in peer capability advertisement that degrades peer discovery quality across the network. Nodes seeking Discovery-capable peers will repeatedly dial non-Discovery nodes, wasting outbound connection slots and receiving no peer address data in return. The false flags propagate transitively via Discovery `Nodes` responses, polluting peer stores network-wide. This qualifies as **Low (501–2000 points): Any other important performance/correctness improvement for CKB**, as it measurably degrades the efficiency of peer routing without crashing nodes or causing consensus issues.

## Likelihood Explanation
The default `support_protocols` list in `resource/ckb.toml` line 112 includes Discovery, so the bug is dormant in default deployments. It is triggered by any operator who removes Discovery from `support_protocols` — a configuration the codebase explicitly documents as optional (line 111: "only 'Sync' and 'Identify' are mandatory, others are optional"). Light-client-only or relay-only node operators following this documented customization path will silently trigger the mismatch. The false flag is set at startup and never corrected, so every session for the lifetime of the process carries the incorrect advertisement.

## Recommendation
Add the missing `else` branch for `Flags::DISCOVERY` in `util/launcher/src/lib.rs`, immediately after the existing `LightClient` block, mirroring the pattern used for `RELAY`, `BLOCK_FILTER`, and `LIGHT_CLIENT`:

```rust
if support_protocols.contains(&SupportProtocol::Discovery) {
    // Discovery is registered inside NetworkService::new()
} else {
    flags.remove(Flags::DISCOVERY);
}
```

## Proof of Concept
1. Set `support_protocols = ["Ping", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert"]` (Discovery omitted) in `ckb.toml`.
2. Start node A. Confirm `NetworkService::new()` skips the Discovery registration block (`network/src/network.rs` lines 896–914).
3. Connect node B to node A. Node B's `received_identify` handler receives an Identify message from A with `flags` containing `Flags::DISCOVERY` (bit `0b10` set), because `flags = Flags::all()` and `Flags::DISCOVERY` is never cleared.
4. Inspect node B's peer store: node A's address is stored with `Flags::DISCOVERY` set via `peer_store.add_outbound_addr` (`network/src/protocols/identify/mod.rs` line 422).
5. Connect node C to node B and issue a `GetNodes` with `required_flags = Flags::DISCOVERY`. Node B returns node A's address via `fetch_random_addrs` (`peer_store_impl.rs` line 269).
6. Node C dials node A; Sync opens but Discovery never opens, confirming the false advertisement. The root cause is the missing `flags.remove(Flags::DISCOVERY)` branch at `util/launcher/src/lib.rs` lines 428–476, while analogous removals for `RELAY`, `BLOCK_FILTER`, and `LIGHT_CLIENT` are all present.