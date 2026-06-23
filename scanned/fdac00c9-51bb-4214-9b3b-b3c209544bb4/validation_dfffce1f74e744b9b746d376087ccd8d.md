Now I have all the code I need. Let me trace the full attack path precisely.

### Title
Unauthenticated Inbound Peer Can Poison `observed_addrs` and Cause the Local Node to Broadcast Arbitrary IP:Port as Its Own Listen Address — (`network/src/protocols/identify/mod.rs`)

---

### Summary

An unprivileged inbound peer can send a crafted `observed_addr` in its `IdentifyMessage` pointing to any arbitrary IP:port. The local node stores this address without any validation and subsequently re-broadcasts it to every new connecting peer as one of its own listen addresses, violating the invariant that a node only advertises its own verified endpoints.

---

### Finding Description

The attack path is fully traceable through production code:

**Step 1 — Attacker sends crafted `observed_addr`.**

When the local node receives an `IdentifyMessage`, `received()` calls `process_observed()` with the raw `observed_addr` from the wire: [1](#0-0) 

`process_observed` performs zero validation on the address content and immediately delegates to the callback: [2](#0-1) 

**Step 2 — `add_observed_addr` appends local peer_id and stores unconditionally.**

The `IdentifyCallback::add_observed_addr` implementation only checks whether a peer_id component is already present. If not, it appends the **local node's own peer_id** — making the attacker-supplied IP:port look like a legitimate self-address. It then stores it with no IP-source validation: [3](#0-2) 

`NetworkState::add_observed_addr` is a blind insert with no further checks: [4](#0-3) 

**Step 3 — Poisoned address is included in `local_listen_addrs()` for subsequent peers.**

`listen_addrs()` returns at most `MAX_RETURN_LISTEN_ADDRS` (10) entries from `public_addrs`. If the local node has fewer than 10 configured public addresses (the common case for NAT nodes), `local_listen_addrs()` fills the remaining slots from `observed_addrs`: [5](#0-4) 

**Step 4 — Poisoned address passes the `is_reachable` filter and is sent to the next peer.**

In `connected()`, the output of `local_listen_addrs()` is filtered by `is_reachable()`. A victim node's public IP is reachable by definition, so the poisoned address passes and is encoded into the `IdentifyMessage` sent to every subsequent connecting peer: [6](#0-5) 

**The missing guard:** there is no check that the IP in `observed_addr` matches the actual remote IP of the session that sent it. The attacker at `1.2.3.4` can claim the local node is reachable at `5.6.7.8:<port>`.

---

### Impact Explanation

- The local node advertises the victim's IP:port (with its own peer_id appended) as one of its own listen addresses to all subsequent peers.
- Peers receiving this poisoned address will attempt to connect to the victim's IP expecting the local node's peer_id. The TLS/secio handshake will fail (wrong peer_id), but the connection attempt still reaches the victim.
- With multiple attackers or repeated reconnections, the local node's advertised address set becomes entirely poisoned, making it appear unreachable to the rest of the network.
- The victim node receives a stream of unsolicited connection attempts from every peer that received the poisoned `IdentifyMessage`, constituting a reflected/amplified connection-flood.
- Because `observed_addrs` is keyed by `SessionId` and cleaned up on disconnect, the attacker can maintain the poison by staying connected, or re-inject it by reconnecting.

---

### Likelihood Explanation

- Requires only a standard inbound TCP connection — no special privileges, no PoW, no key material.
- The precondition (`public_addrs.len() < MAX_RETURN_LISTEN_ADDRS`) is satisfied by any node that has not explicitly configured 10 or more `public_addresses`, which is the default for most deployments.
- The `IdentifyMessage` is the very first message exchanged on every new session, so the injection window is always open.
- The attack is locally testable and requires no Sybil capability.

---

### Recommendation

In `add_observed_addr`, validate that the IP component of the supplied `observed_addr` matches the actual remote IP of the session (available from `SessionContext::address`). Reject or ignore any `observed_addr` whose IP does not match the session's transport-layer source address. This is the standard mitigation used by libp2p's identify protocol.

---

### Proof of Concept

```
1. Start a CKB node N with no configured public_addresses (public_addrs is empty).
2. Connect as inbound peer A (any TCP client that speaks the tentacle/secio handshake).
3. After the secio handshake, send an IdentifyMessage with:
     observed_addr = /ip4/<victim_ip>/tcp/<victim_port>/p2p/<N_peer_id>
   (the p2p component can be omitted; add_observed_addr will append N's peer_id automatically)
4. Keep peer A connected.
5. Connect as a second inbound peer B.
6. Capture the IdentifyMessage that N sends to B.
7. Assert that listen_addrs in that message contains /ip4/<victim_ip>/tcp/<victim_port>/p2p/<N_peer_id>.
8. Observe that B (and all subsequent peers) will attempt to connect to <victim_ip>:<victim_port>.
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L152-169)
```rust
    fn process_observed(
        &mut self,
        context: &mut ProtocolContextMutRef,
        observed: Multiaddr,
    ) -> MisbehaveResult {
        debug!(
            "IdentifyProtocol process observed address, session: {:?}, observed: {}",
            context.session, observed,
        );

        let session = context.session;
        let info = self
            .remote_infos
            .get_mut(&session.id)
            .expect("RemoteInfo must exists");
        self.callback.add_observed_addr(observed, info.session.id);
        MisbehaveResult::Continue
    }
```

**File:** network/src/protocols/identify/mod.rs (L214-232)
```rust
            self.callback
                .local_listen_addrs()
                .iter()
                .filter(|addr| {
                    if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
                        !self.global_ip_only || is_reachable(socket_addr.ip())
                    } else {
                        // allow /onion3 address
                        addr.iter()
                            .any(|protocol| matches!(protocol, Protocol::Onion3(_)))
                    }
                })
                .take(MAX_ADDRS)
                .cloned()
                .collect()
        };

        let identify = self.callback.identify();
        let data = IdentifyMessage::new(listen_addrs, session.address.clone(), identify).encode();
```

**File:** network/src/protocols/identify/mod.rs (L290-298)
```rust
                if let MisbehaveResult::Disconnect =
                    self.process_observed(&mut context, message.observed_addr.clone())
                {
                    error!(
                        "Disconnect IdentifyProtocol session {:?} due to invalid observed addr: {}.",
                        session, message.observed_addr,
                    );
                    let _ = context.disconnect(session.id).await;
                }
```

**File:** network/src/protocols/identify/mod.rs (L458-470)
```rust
    fn local_listen_addrs(&mut self) -> Vec<Multiaddr> {
        let mut listens = self.listen_addrs();

        if listens.len() < MAX_RETURN_LISTEN_ADDRS {
            let observe_addrs = self
                .network_state
                .observed_addrs(MAX_RETURN_LISTEN_ADDRS - listens.len());
            listens.extend(observe_addrs);
            listens
        } else {
            listens
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L497-507)
```rust
    fn add_observed_addr(&mut self, mut addr: Multiaddr, session_id: SessionId) -> MisbehaveResult {
        if extract_peer_id(&addr).is_none() {
            addr.push(Protocol::P2P(Cow::Borrowed(
                self.network_state.local_peer_id().as_bytes(),
            )))
        }

        self.network_state.add_observed_addr(session_id, addr);
        // NOTE: for future usage
        MisbehaveResult::Continue
    }
```

**File:** network/src/network.rs (L513-516)
```rust
    pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
        let mut pending_observed_addrs = self.observed_addrs.write();
        pending_observed_addrs.insert(session_id, addr);
    }
```
