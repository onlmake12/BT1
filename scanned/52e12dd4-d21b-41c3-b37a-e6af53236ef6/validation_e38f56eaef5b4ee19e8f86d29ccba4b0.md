Audit Report

## Title
Unvalidated `observed_addr` Injection Poisons Node's Own Address Advertisement - (File: `network/src/protocols/identify/mod.rs`)

## Summary
Any peer that completes a P2P handshake with a victim node can inject an arbitrary globally-reachable `Multiaddr` into the victim's `observed_addrs` store by sending a crafted `IdentifyMessage`. The victim then unconditionally re-advertises this attacker-controlled address as its own listen address to every subsequent peer it connects to, and the address also propagates through the hole-punching gossip path. Critically, `observed_addrs` entries are never removed on session disconnect, so the poisoned entry persists indefinitely.

## Finding Description

**Step 1 — Injection with no source-IP validation.**
`IdentifyProtocol::process_observed` (L152–169) accepts the `observed_addr` field from the remote peer's `IdentifyMessage` and passes it directly to `add_observed_addr` without comparing it to `context.session.address` (the transport-layer source address of that session):

```rust
// L167
self.callback.add_observed_addr(observed, info.session.id);
```

`IdentifyCallback::add_observed_addr` (L497–507) appends the local peer-id if absent and writes the address into the shared map:

```rust
self.network_state.add_observed_addr(session_id, addr);
```

`NetworkState::add_observed_addr` (L512–516) inserts unconditionally:

```rust
pending_observed_addrs.insert(session_id, addr);
```

**Step 2 — Existing filter is insufficient.**
`IdentifyProtocol::connected` (L217–224) filters outbound `listen_addrs` with `is_reachable`, which only rejects private/loopback IPs. Any globally-routable IP the attacker controls passes this check. There is no check that the IP matches the session's actual remote IP.

**Step 3 — Poisoned address propagates to all subsequent peers.**
`local_listen_addrs` (L457–470) appends `observed_addrs` to the node's own listen addresses whenever it connects to a new peer. The result is sent in every outgoing `IdentifyMessage` (L232).

**Step 4 — Propagation through hole-punching.**
`hole_punching/mod.rs` (L190–198) also reads `observed_addrs` and includes them in `ConnectionRequest` gossip broadcast to multiple peers.

**Step 5 — No cleanup on disconnect.**
`disconnected` (L239–248) removes the session from `remote_infos` but there is no corresponding removal from `observed_addrs`. A grep for any `remove` call on `observed_addrs` returns no matches. The poisoned entry persists until the node restarts.

**Step 6 — Duplicate-message guard does not help.**
`check_duplicate` (L107–121) prevents a peer from sending more than one `IdentifyMessage` per session, but the attacker only needs one message. After disconnect, the attacker can reconnect and overwrite the entry for the new `SessionId`.

## Impact Explanation

**High — bad design which could cause CKB network congestion with few costs.**

A single unprivileged attacker with one TCP connection can:
1. Poison the victim's `observed_addrs` with an attacker-controlled globally-routable IP.
2. Cause the victim to advertise that IP as its own listen address to every peer it subsequently connects to (up to `MAX_RETURN_LISTEN_ADDRS = 10` addresses per message).
3. Cause those peers to write the attacker's address into their peer stores under the victim's peer-id (`add_remote_listen_addrs` → `peer_store.add_addr`).
4. Cause downstream peers to attempt connections to the attacker's address, wasting connection slots and degrading network connectivity.
5. Amplify the reach via hole-punching gossip to `sqrt(N)` peers per notify interval.

Because the poisoned entry never expires, a brief connection is sufficient to cause lasting, network-wide address table corruption. At scale (multiple victims, multiple attacker connections), this degrades the P2P overlay's ability to route connections, constituting network congestion with minimal attacker cost.

## Likelihood Explanation

- **Entry requirement**: Any node that can establish a TCP connection and complete the secio handshake. No special privilege, no pre-existing trust.
- **Effort**: One connection, one `IdentifyMessage`. The message is sent immediately on protocol open.
- **Persistence**: The poisoned entry survives session close and is re-broadcast indefinitely.
- **Repeatability**: The attacker can reconnect after disconnect to refresh or change the injected address.
- **Amplification**: Via hole-punching gossip, the address reaches peers beyond the victim's direct connections.

## Recommendation

**Short term**: In `process_observed` / `add_observed_addr`, extract the IP component from the supplied `observed_addr` and compare it to the transport-layer source IP of the session (`context.session.address`). Reject any `observed_addr` whose IP does not match the session's actual remote IP.

**Medium term**: Remove the `observed_addrs` entry for a `SessionId` when that session disconnects (add a `remove` call in `disconnected` or in `NetworkState`).

**Long term**: Treat `observed_addrs` as hints only. Require corroboration from multiple independent peers (different IPs) before including an observed address in outbound `listen_addrs`. This is the approach used by Bitcoin Core's address manager and libp2p's identify-push extension.

## Proof of Concept

1. Attacker Eve establishes a TCP connection to victim node V and completes the secio handshake.
2. Eve sends an `IdentifyMessage` with:
   - `observed_addr` = `/ip4/<eve_server_ip>/tcp/8114` (a globally-routable IP Eve controls, distinct from Eve's actual connection IP)
   - `listen_addrs` = any valid addresses
   - `identify` = valid network identifier bytes (required to pass `received_identify`)
3. V's `received` handler calls `process_observed` → `add_observed_addr` → `NetworkState::add_observed_addr`, storing `<eve_server_ip>:8114` in `observed_addrs[eve_session_id]`.
4. Eve disconnects. The entry remains in `observed_addrs` (no cleanup).
5. V subsequently connects to peer P. `connected` calls `local_listen_addrs`, which appends `<eve_server_ip>:8114` (passes `is_reachable` check). V sends this to P in its `IdentifyMessage`.
6. P calls `add_remote_listen_addrs`, writing `<eve_server_ip>:8114` into its peer store under V's peer-id.
7. P later dials `<eve_server_ip>:8114` expecting V; Eve's server answers instead.
8. Repeat from step 1 with additional victim nodes to scale the attack.