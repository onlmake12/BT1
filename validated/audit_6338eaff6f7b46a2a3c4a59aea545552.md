Audit Report

## Title
COMPATIBILITY-Flag Bypass in `required_flags_filter` Allows Outbound Slot Exhaustion Without Providing Services — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`required_flags_filter` short-circuits to `true` for any peer whose flags include `Flags::COMPATIBILITY` (0b1) when the required set is `RELAY | DISCOVERY | SYNC`, regardless of whether those flags are actually present. An attacker advertising only `Flags::COMPATIBILITY` passes both the address-selection filter and the post-identify protocol-opening check, consuming a victim node's outbound connection slot while providing no sync, relay, or discovery service.

## Finding Description

The root cause is confirmed at [1](#0-0)  — when `required == RELAY | DISCOVERY | SYNC`, the function returns `true` for any `t` that contains `Flags::COMPATIBILITY` (0b1), even if `SYNC`, `DISCOVERY`, and `RELAY` are all absent.

`Flags::COMPATIBILITY = 0b1` is confirmed at [2](#0-1) 

**Step 1 — Address selection.** `OutboundPeerService::try_dial_peers` calls `fetch_addrs_to_attempt` with `*target` (the node's `required_flags`, defaulting to `RELAY | DISCOVERY | SYNC`). [3](#0-2)  Because `required_flags_filter(RELAY|DISCOVERY|SYNC, COMPATIBILITY)` returns `true`, a COMPATIBILITY-only address stored in the peer store is selected for dialing.

**Step 2 — Identify verification.** The `verify` function only rejects `flag == 0`; `flag = 0b1` passes. [4](#0-3) 

**Step 3 — Protocol opening.** After the identify exchange, `required_flags_filter` is called again. Because the bypass fires, the `else if` branch at L434 is taken and `open_protocols(Filter)` opens every non-Feeler protocol on the outbound session. [5](#0-4)  The comment "The remote end can support all local protocols" is factually incorrect for a COMPATIBILITY-only peer, confirming this is unintended. [6](#0-5) 

The disconnect path at L444–450 is never reached for COMPATIBILITY-only peers, making the guard ineffective. [7](#0-6) 

## Impact Explanation

This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker operating enough nodes to fill a victim's `max_outbound` quota prevents the victim from establishing outbound connections to real full nodes, halting chain synchronization. The victim continues sending compact blocks and transactions to attacker nodes that never reciprocate. The attack is self-sustaining because once attacker addresses enter the peer store via discovery, the victim re-dials them after each disconnection.

## Likelihood Explanation

No privileged access is required. Any node on the P2P network can advertise `Flags::COMPATIBILITY` in its identify message. Address propagation via the discovery protocol is the standard mechanism — no Sybil infrastructure is needed to seed a few attacker addresses. The attacker only needs to respond to the identify handshake with a valid network name and `flag = 0b1`. The attack is cheap, repeatable, and self-sustaining.

## Recommendation

Remove the `Flags::COMPATIBILITY` short-circuit from `required_flags_filter`, or restrict it to a separate legacy-detection path that does not grant full protocol access. A peer advertising only `COMPATIBILITY` should fail the `required == RELAY | DISCOVERY | SYNC` check and be disconnected via the `else` branch, exactly as the existing disconnect logic at L444–450 intends. If backward compatibility with old nodes genuinely requires the bypass, it should be gated on an explicit legacy-mode configuration flag, not applied unconditionally.

## Proof of Concept

1. Run a modified CKB node that sends `flag = 0b1` (`Flags::COMPATIBILITY`) and the correct network name in its identify message.
2. Propagate its address to a victim node via the discovery protocol (or inject it directly into the victim's peer store).
3. Wait for `OutboundPeerService::try_dial_peers` to invoke `fetch_addrs_to_attempt` — the address is selected because `required_flags_filter(RELAY|DISCOVERY|SYNC, COMPATIBILITY)` returns `true`.
4. When the victim dials and the identify exchange completes, observe that `open_protocols(Filter)` is called and all non-Feeler protocols are opened on the outbound session.
5. Repeat with enough attacker nodes to fill `max_outbound`; the victim can no longer establish outbound connections to real full nodes and chain synchronization halts.

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

**File:** network/src/protocols/identify/mod.rs (L444-450)
```rust
                    } else {
                        // The remote end cannot support all local protocols.
                        warn!(
                            "Session closed from IdentifyProtocol due to peer's flag not meeting the requirements"
                        );
                        return MisbehaveResult::Disconnect;
                    }
```

**File:** network/src/protocols/identify/mod.rs (L553-556)
```rust
        let flag: u64 = reader.flag().into();
        if flag == 0 {
            return None;
        }
```

**File:** network/src/protocols/identify/mod.rs (L568-570)
```rust
        /// Compatibility reserved
        const COMPATIBILITY = 0b1;
        /// Discovery protocol, which can provide peers data service
```

**File:** network/src/services/outbound_peer.rs (L109-124)
```rust
        let target = &self.network_state.required_flags;

        let filter = |peer_addr: &AddrInfo| match self.transport_type {
            TransportType::Tcp => true,
            TransportType::Ws => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_) | Protocol::Tcp(_))),
            TransportType::Wss => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_))),
        };

        let f = |peer_store: &mut PeerStore, number: usize, now_ms: u64| -> Vec<AddrInfo> {
            let paddrs = peer_store.fetch_addrs_to_attempt(number, *target, filter);
```
