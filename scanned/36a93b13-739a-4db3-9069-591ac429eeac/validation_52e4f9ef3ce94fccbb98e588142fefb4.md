Audit Report

## Title
Peer Flag Filter OR Bypass Allows COMPATIBILITY-Only Peers to Satisfy RELAY|DISCOVERY|SYNC Requirements — (File: `network/src/peer_store/peer_store_impl.rs`)

## Summary
The `required_flags_filter` function at `network/src/peer_store/peer_store_impl.rs:407-413` contains an OR branch that allows a peer advertising only `COMPATIBILITY` (`0b1`) to pass a filter requiring `RELAY | DISCOVERY | SYNC` (`0b1110`). This bypass is active at all three call sites: outbound peer selection (`fetch_addrs_to_attempt`), discovery address sharing (`fetch_random_addrs`), and the post-identify protocol-open decision in `network/src/protocols/identify/mod.rs:434`. An unprivileged attacker running nodes that advertise only `flag = 0b1` can occupy honest nodes' outbound connection slots, cause those nodes to open SYNC/RELAY/DISCOVERY protocols toward non-supporting peers, and propagate the attacker's addresses network-wide via discovery responses.

## Finding Description
**Root cause:** `required_flags_filter` at `peer_store_impl.rs:407-413`:
```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    if required == Flags::RELAY | Flags::DISCOVERY | Flags::SYNC {
        t.contains(required) || t.contains(Flags::COMPATIBILITY)
    } else {
        t.contains(required)
    }
}
```
`Flags::COMPATIBILITY = 0b1`. A peer with `flags = 0b1` does not contain `RELAY | DISCOVERY | SYNC` (`0b1110`), but `t.contains(Flags::COMPATIBILITY)` evaluates to `true`, so the function returns `true` for a COMPATIBILITY-only peer whenever `required = RELAY | DISCOVERY | SYNC`.

**Call site 1 — outbound peer selection** (`peer_store_impl.rs:201-212`): `fetch_addrs_to_attempt` calls `required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))`. A COMPATIBILITY-only peer passes this filter and is returned as a candidate for outbound connection.

**Call site 2 — discovery address sharing** (`peer_store_impl.rs:276-282`): `fetch_random_addrs` uses the same filter. When a remote peer sends `GetNodes` with `required_flags = RELAY | DISCOVERY | SYNC`, the node returns COMPATIBILITY-only peers as valid candidates, spreading them across the network.

**Call site 3 — identify protocol handler** (`identify/mod.rs:434-443`): After the identify handshake, `required_flags_filter(required_flags, flags)` is called with the flags received from the peer. A peer sending `flags = COMPATIBILITY` passes this check, causing the honest node to open all non-Feeler protocols (SYNC, RELAY, DISCOVERY) toward a peer that supports none of them.

**Address propagation amplifier** (`identify/mod.rs:472-494`): `add_remote_listen_addrs` stores the attacker's advertised listen addresses in the peer store with `flags = COMPATIBILITY`. These addresses then pass `required_flags_filter` in `fetch_random_addrs`, causing them to be included in `GetNodes` responses sent to third-party nodes.

**Existing checks are insufficient:** The `verify` function in `Identify` (`identify/mod.rs:541-561`) only rejects `flag == 0`; `flag = 1` (COMPATIBILITY) passes. The peer store scoring/eviction system penalizes misbehavior but does not prevent the initial slot occupation and address propagation. The attacker can rotate nodes to avoid persistent bans.

## Impact Explanation
This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker running multiple COMPATIBILITY-only nodes can: (1) occupy outbound connection slots on honest nodes, preventing legitimate SYNC/RELAY/DISCOVERY peers from connecting; (2) cause honest nodes to waste resources attempting to open unsupported protocols; (3) propagate attacker addresses network-wide via discovery, causing other honest nodes to also waste slots. At scale, this degrades block propagation and transaction relay across the CKB P2P network. The cost to the attacker is minimal (running modified nodes with crafted identify messages), and the bypass is deterministic and repeatable.

## Likelihood Explanation
The attack requires only running a TCP server that speaks the CKB p2p handshake and sends `flag = 1` in its identify message — no keys, no hashpower, no privileged access. The bypass is deterministic (no brute force). A single attacker node wastes at least one connection slot per victim and propagates its address to further nodes. A handful of attacker nodes can meaningfully degrade connectivity. The attacker's nodes can be discovered organically or by directly connecting to honest nodes.

## Recommendation
Remove the COMPATIBILITY bypass from `required_flags_filter` when used for peer selection and protocol opening. The simplest fix:
```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    t.contains(required)
}
```
If backward compatibility with genuinely old nodes must be preserved, apply the COMPATIBILITY bypass only during the pre-identify feeler phase (before `received_identify` completes), then enforce strict flag requirements post-identify. Alternatively, deprecate `COMPATIBILITY` entirely and require all peers to advertise explicit protocol flags.

## Proof of Concept
1. Run a TCP server implementing the CKB p2p handshake that sends an identify message with `flag = 1` (COMPATIBILITY only, no RELAY/DISCOVERY/SYNC bits).
2. Connect this server to an honest CKB full node so it is added to the peer store via `add_outbound_addr` with `flags = COMPATIBILITY`.
3. Verify deterministically: `required_flags_filter(Flags::RELAY | Flags::DISCOVERY | Flags::SYNC, Flags::COMPATIBILITY)` — the condition `t.contains(Flags::COMPATIBILITY)` fires at `peer_store_impl.rs:409`, returning `true`.
4. Observe that `fetch_addrs_to_attempt` returns the attacker's address when selecting outbound peers.
5. Observe that the honest node calls `open_protocols` with `TargetProtocol::Filter` (excluding only Feeler) toward the attacker at `identify/mod.rs:436-443`.
6. Observe that `fetch_random_addrs` includes the attacker's address in `GetNodes` responses, confirming network-wide propagation.
7. Unit test: assert `required_flags_filter(Flags::RELAY | Flags::DISCOVERY | Flags::SYNC, Flags::COMPATIBILITY) == true` and `required_flags_filter(Flags::RELAY | Flags::DISCOVERY | Flags::SYNC, Flags::RELAY | Flags::DISCOVERY | Flags::SYNC) == true`, confirming both branches return `true` while only the second is legitimate.