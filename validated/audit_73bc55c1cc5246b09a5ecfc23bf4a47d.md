Audit Report

## Title
Unvalidated `observed_addr` Injection Poisons Node's Own Address Advertisement - (File: `network/src/protocols/identify/mod.rs`)

## Summary
The Identify protocol's `received` handler accepts an `observed_addr` from any connected peer and stores it unconditionally in `NetworkState::observed_addrs` without verifying that the address matches the session's actual transport-layer source IP. These stored addresses are then appended to the node's own `listen_addrs` and broadcast to every subsequent peer via `IdentifyMessage`, and also propagated through the hole-punching protocol. A single unprivileged peer can therefore inject an arbitrary public IP into the victim's address advertisement with one message.

## Finding Description

**Root cause:** `process_observed` passes the remote-supplied `Multiaddr` directly to `add_observed_addr` with no comparison against `context.session.address`:

```rust
// network/src/protocols/identify/mod.rs L152-169
fn process_observed(&mut self, context: &mut ProtocolContextMutRef, observed: Multiaddr) -> MisbehaveResult {
    let session = context.session;
    let info = self.remote_infos.get_mut(&session.id).expect("RemoteInfo must exists");
    self.callback.add_observed_addr(observed, info.session.id);  // no IP validation
    MisbehaveResult::Continue
}
```

`IdentifyCallback::add_observed_addr` appends the local peer-id if absent and writes unconditionally:

```rust
// network/src/protocols/identify/mod.rs L497-507
fn add_observed_addr(&mut self, mut addr: Multiaddr, session_id: SessionId) -> MisbehaveResult {
    if extract_peer_id(&addr).is_none() {
        addr.push(Protocol::P2P(Cow::Borrowed(self.network_state.local_peer_id().as_bytes())))
    }
    self.network_state.add_observed_addr(session_id, addr);
    MisbehaveResult::Continue
}
```

`NetworkState::add_observed_addr` writes directly into the shared map:

```rust
// network/src/network.rs L512-516
pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
    let mut pending_observed_addrs = self.observed_addrs.write();
    pending_observed_addrs.insert(session_id, addr);
}
```

**Propagation path:** `local_listen_addrs` appends observed addresses when the real listen count is below `MAX_RETURN_LISTEN_ADDRS` (10):

```rust
// network/src/protocols/identify/mod.rs L457-470
fn local_listen_addrs(&mut self) -> Vec<Multiaddr> {
    let mut listens = self.listen_addrs();
    if listens.len() < MAX_RETURN_LISTEN_ADDRS {
        let observe_addrs = self.network_state.observed_addrs(MAX_RETURN_LISTEN_ADDRS - listens.len());
        listens.extend(observe_addrs);
        listens
    } else { listens }
}
```

This result is sent to every new peer in `connected`:

```rust
// network/src/protocols/identify/mod.rs L211-232
let listen_addrs = if self.callback.register(&context, version) {
    Vec::new()
} else {
    self.callback.local_listen_addrs().iter()
        .filter(|addr| {
            if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
                !self.global_ip_only || is_reachable(socket_addr.ip())  // only filters private IPs
            } else { addr.iter().any(|p| matches!(p, Protocol::Onion3(_))) }
        })
        .take(MAX_ADDRS).cloned().collect()
};
```

The `is_reachable` filter only rejects private/loopback IPs; any attacker-controlled **public** IP passes. The poisoned address is also included in hole-punching `ConnectionRequest` broadcasts:

```rust
// network/src/protocols/hole_punching/mod.rs L190-198
let observed_addrs = self.network_state.observed_addrs(ADDRS_COUNT_LIMIT - public_addr.len());
let iter = public_addr.iter().chain(observed_addrs.iter()) ...
```

**Existing guards reviewed and found insufficient:**
- `check_duplicate` prevents a peer from sending a second `IdentifyMessage` in the same session, but the attacker only needs one message per session and can reconnect.
- `global_ip_only` / `is_reachable` filtering in `connected` only rejects RFC-1918/loopback addresses; any routable public IP the attacker controls passes.
- There is no cross-session corroboration requirement before an observed address is trusted.

## Impact Explanation

**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with a single TCP connection can inject a public IP they control into the victim's `observed_addrs`. The victim then advertises this address as its own to every subsequent peer it connects to. Receiving peers call `add_remote_listen_addrs`, which writes the attacker's address into their peer stores under the victim's peer-id. Those peers later attempt to dial the attacker's address, wasting connection slots and bandwidth. Via hole-punching gossip, the poisoned address reaches `O(sqrt(N))` additional peers per notify interval. The cost to the attacker is one TCP connection and one crafted message; the resulting wasted dial attempts and degraded peer discovery scale with the victim's connectivity.

## Likelihood Explanation

- **Entry path:** Any peer that can establish a TCP connection (inbound or outbound) qualifies. No authentication beyond the standard P2P handshake is required.
- **Ease:** The attacker sends exactly one crafted `IdentifyMessage` immediately after the protocol opens. The `check_duplicate` guard prevents a second message in the same session, but the attacker can reconnect to refresh the entry.
- **Persistence:** The poisoned entry remains in `observed_addrs` for the entire session lifetime and is broadcast to every new peer the victim connects to during that window.
- **Amplification:** Hole-punching gossip propagates the address to additional peers without further attacker involvement.

## Recommendation

**Short term:** In `process_observed`, extract the IP component of the remote-supplied `observed` address and compare it against the transport-layer source IP of the session (`context.session.address`). Reject (return `MisbehaveResult::Disconnect` or silently ignore) any `observed_addr` whose IP does not match the session's actual remote IP.

**Long term:** Treat `observed_addrs` as unconfirmed hints. Require corroboration from at least N independent peers reporting the same address before including it in outbound `listen_addrs`. This is the approach used by Bitcoin Core's address manager and libp2p's identify-push extension.

## Proof of Concept

1. Attacker Eve establishes a TCP connection to victim node V (inbound or outbound).
2. After the secio handshake, Eve sends an `IdentifyMessage` with:
   - `observed_addr` = `/ip4/<attacker-public-ip>/tcp/8114` (Eve's server, not V's real address)
   - `listen_addrs` = any syntactically valid addresses
   - `identify` = valid network identifier bytes
3. V's `received` handler calls `process_observed` → `add_observed_addr` → `NetworkState::add_observed_addr`, storing `<attacker-public-ip>:8114/p2p/<V-peer-id>` in `observed_addrs[eve_session_id]`.
4. V subsequently connects to peer P. `connected` calls `local_listen_addrs`, which appends the poisoned address (it passes `is_reachable` because it is a public IP), and sends it to P in V's `IdentifyMessage`.
5. P calls `add_remote_listen_addrs`, writing `<attacker-public-ip>:8114` into its peer store under V's peer-id.
6. P later dials `<attacker-public-ip>:8114` expecting V; the connection reaches Eve's server. The P2P peer-id handshake fails (Eve lacks V's private key), wasting P's connection slot and degrading network connectivity.
7. Repeat step 4–6 for every peer V connects to during Eve's session.