All cited code locations are confirmed against the actual repository. Here is the validated finding:

---

Audit Report

## Title
Peer Flag Bypass via `COMPATIBILITY`-Only Advertisement Allows Outbound Slot Exhaustion — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`required_flags_filter` unconditionally accepts `Flags::COMPATIBILITY` (0b1) as satisfying the `SYNC|DISCOVERY|RELAY` outbound filter. An unprivileged attacker can exploit the feeler→full-outbound promotion pipeline to fill all outbound connection slots with peers that advertise only `COMPATIBILITY`, causing sync stall and effective network isolation of the victim node.

## Finding Description

**Root cause** — `required_flags_filter` at `network/src/peer_store/peer_store_impl.rs` L407–413:

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    if required == Flags::RELAY | Flags::DISCOVERY | Flags::SYNC {
        t.contains(required) || t.contains(Flags::COMPATIBILITY)
    } else {
        t.contains(required)
    }
}
```

`COMPATIBILITY` alone satisfies the filter when `required == SYNC|DISCOVERY|RELAY`. All six exploit steps are confirmed in production code:

**Step 1 — Seed the peer store.** `add_remote_listen_addrs` (`identify/mod.rs` L472–494) stores the attacker's listen addresses with `flags = COMPATIBILITY` when `identify_info` is absent (`peer.identify_info.as_ref().map(|a| a.flags).unwrap_or(Flags::COMPATIBILITY)`).

**Step 2 — Feeler selection.** `fetch_addrs_to_feeler` (`peer_store_impl.rs` L217–240) applies no flag filter — only connectivity timing checks. The attacker's address is eligible.

**Step 3 — Feeler dial and identify.** During identify, `received_identify` (`identify/mod.rs` L425–433) calls `change_feeler_flags(&addr, COMPATIBILITY)` → returns `true` (it is a feeler) → opens the Feeler protocol.

**Step 4 — `Feeler::connected` stores the addr.** `feeler.rs` L30–39: reads `feeler_flags` (now `COMPATIBILITY`) and calls `peer_store.add_outbound_addr(addr, COMPATIBILITY)`, setting `last_connected_at_ms = now` via `AddrInfo::new(addr, ckb_systemtime::unix_time_as_millis(), ...)`.

**Step 5 — `fetch_addrs_to_attempt` selects the attacker.** `peer_store_impl.rs` L201–209: the filter checks `required_flags_filter(SYNC|DISCOVERY|RELAY, COMPATIBILITY)` → `true`, and `last_connected_at_ms` is recent → attacker's address is returned.

**Step 6 — Full outbound connection.** In `received_identify` (`identify/mod.rs` L434–443): `change_feeler_flags` returns `false` (not a feeler), then `required_flags_filter(SYNC|DISCOVERY|RELAY, COMPATIBILITY)` → `true` → all non-feeler protocols are opened. The attacker is now a full outbound peer.

**Existing guards are insufficient:** `fetch_addrs_to_attempt` only checks (1) not already connected, (2) connected within 3 days, and (3) `required_flags_filter`. The `required_flags_filter` unconditionally passes `COMPATIBILITY` for the standard full-node `required_flags`, with no epoch check or additional gate.

## Impact Explanation

An attacker with enough reachable IPs can fill all outbound slots with `COMPATIBILITY`-only peers that serve no sync, relay, or discovery traffic. The victim node cannot download new blocks or propagate transactions. Scaled across multiple victim nodes, this constitutes **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* (10001–15000 points).

## Likelihood Explanation

The attack is reachable by any unprivileged external user. The attacker only needs to be dialable and have their address seeded into the victim's peer store — achievable via a single inbound connection or discovery propagation. The bypass in `required_flags_filter` is unconditional and applies to every standard full-node deployment where `required_flags = SYNC|DISCOVERY|RELAY`. The attack is repeatable and scales with the number of attacker-controlled IPs relative to the victim's outbound slot count.

## Recommendation

Remove the `COMPATIBILITY` bypass from `required_flags_filter`:

```rust
pub(crate) fn required_flags_filter(required: Flags, t: Flags) -> bool {
    t.contains(required)
}
```

If backward compatibility with legacy nodes must be preserved, gate the bypass on a network-level epoch check rather than a static flag comparison, and ensure `COMPATIBILITY`-only peers are never promoted from feeler to full outbound peers when `required_flags` includes `SYNC|DISCOVERY|RELAY`.

## Proof of Concept

```rust
use crate::{Flags, peer_store::peer_store_impl::required_flags_filter};

#[test]
fn test_compatibility_bypasses_sync_relay_discovery() {
    let required = Flags::SYNC | Flags::RELAY | Flags::DISCOVERY;
    let attacker_flags = Flags::COMPATIBILITY;
    // Asserts true — confirms the bypass
    assert!(required_flags_filter(required, attacker_flags));
}
```

The full end-to-end path (inbound seed → `fetch_addrs_to_feeler` → feeler dial → `add_outbound_addr(COMPATIBILITY)` → `fetch_addrs_to_attempt` → `dial_identify` → full outbound peer) is traceable entirely through production code at the cited locations. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L102-114)
```rust
    /// Add outbound peer address
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

**File:** network/src/peer_store/peer_store_impl.rs (L217-240)
```rust
    pub fn fetch_addrs_to_feeler<F>(&mut self, count: usize, filter: F) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
        // Get info:
        // 1. Not already connected
        // 2. Not already tried in a minute
        // 3. Not connected within 3 days

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let addr_expired_ms = now_ms.saturating_sub(ADDR_TRY_TIMEOUT_MS);
        let peers = &self.connected_peers;

        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

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

**File:** network/src/protocols/identify/mod.rs (L421-450)
```rust
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
                    } else {
                        // The remote end cannot support all local protocols.
                        warn!(
                            "Session closed from IdentifyProtocol due to peer's flag not meeting the requirements"
                        );
                        return MisbehaveResult::Disconnect;
                    }
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

**File:** network/src/protocols/feeler.rs (L27-48)
```rust
    async fn connected(&mut self, context: ProtocolContextMutRef<'_>, _version: &str) {
        let session = context.session;
        if context.session.ty.is_outbound() {
            let flags = self.network_state.with_peer_registry(|reg| {
                if let Some(p) = reg.feeler_flags(&session.address) {
                    p
                } else {
                    Flags::COMPATIBILITY
                }
            });
            self.network_state.with_peer_store_mut(|peer_store| {
                peer_store.add_outbound_addr(session.address.clone(), flags);
            });
        }

        debug!("peer={} FeelerProtocol.connected", session.address);
        if let Err(err) =
            async_disconnect_with_message(context.control(), session.id, "feeler connection").await
        {
            debug!("Disconnect failed {:?}, error: {:?}", session.id, err);
        }
    }
```
