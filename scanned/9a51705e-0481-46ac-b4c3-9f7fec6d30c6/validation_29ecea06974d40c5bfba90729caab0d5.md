The attack chain is real. Let me confirm the cleanup behavior and the exact conditions.

**Cleanup on disconnect:** `observed_addrs` IS cleaned up on `SessionClose` at `network/src/network.rs:814-817`, so Peer A must remain connected when Peer B connects. This is a trivially satisfiable condition — Peer A simply stays connected.

**Full trace:**

1. Peer A connects, sends `IdentifyMessage` with `observed_addr = /ip4/1.2.3.4/tcp/9999` (any public IP).
2. `received()` → `process_observed()` → `callback.add_observed_addr(observed, session_id)` [1](#0-0) 
3. `IdentifyCallback::add_observed_addr` appends local peer_id if missing, then calls `network_state.add_observed_addr(session_id, addr)` — **no validation that the addr matches the session's actual remote IP**. [2](#0-1) 
4. `NetworkState::add_observed_addr` blindly inserts into the `observed_addrs` HashMap. [3](#0-2) 
5. Peer B connects → `connected()` → `register()` returns `false` for non-anchor peers → `local_listen_addrs()` is called. [4](#0-3) 
6. `local_listen_addrs()` calls `listen_addrs()` (which reads `public_addrs`). For the common case where no `public_addresses` is configured and `listen_addresses` is `0.0.0.0`, `public_addrs` is empty (filtered by `is_reachable`), so `observed_addrs` fills the entire slot up to `MAX_RETURN_LISTEN_ADDRS`. [5](#0-4) 
7. The `is_reachable` filter in `connected()` passes any real public IP injected by Peer A. [6](#0-5) 
8. `IdentifyMessage` sent to Peer B contains the injected address as the node's own listen address. [7](#0-6) 
9. Peer B's `add_remote_listen_addrs` stores the injected address in its peer store, associated with the victim node's peer_id. [8](#0-7) 

---

### Title
Unauthenticated `observed_addr` Injection Causes Peer Store Address Spoofing — (`network/src/protocols/identify/mod.rs`)

### Summary
Any unprivileged inbound peer can inject an arbitrary public IP address into a victim node's `NetworkState::observed_addrs`. This address is then advertised to every subsequently connecting peer as the victim node's own listen address, polluting their peer stores and propagating via the discovery protocol.

### Finding Description
`IdentifyCallback::add_observed_addr` accepts the `observed_addr` field from a remote peer's `IdentifyMessage` without verifying that the address matches the session's actual remote IP. The address is stored in `NetworkState::observed_addrs` keyed by `SessionId`.

`local_listen_addrs()` fills up to `MAX_RETURN_LISTEN_ADDRS = 10` slots first from `public_addrs`, then from `observed_addrs`. For the common deployment where `public_addresses` is not configured and `listen_addresses` is `0.0.0.0/tcp/8115` (not reachable, filtered out), `public_addrs` is empty and `observed_addrs` fills all 10 slots. The only downstream filter is `is_reachable()`, which passes any real public IP.

The `observed_addrs` entry persists until `SessionClose` fires for Peer A's session, so the attack window is the entire duration of Peer A's connection.

### Impact Explanation
- Every peer that connects while Peer A is connected receives the injected address as the victim node's listen address.
- Those peers store the injected address in their peer stores via `add_remote_listen_addrs` → `peer_store.add_addr`.
- The discovery protocol re-broadcasts peer store entries to further peers, propagating the spoofed address network-wide.
- Nodes attempting to connect to the victim node using the injected address will fail, degrading peer connectivity for the victim node.
- The claimed "consensus deviation" is overstated — PoW and block validation are unaffected. The actual impact is **network-level address spoofing and peer store pollution**, which can degrade peer connectivity and block propagation for the victim node.

### Likelihood Explanation
Any peer that can establish a TCP connection to the victim node can trigger this. No special privileges, keys, or majority hashpower are required. The attack is trivially reproducible in a local test environment.

### Recommendation
In `add_observed_addr`, validate that the IP portion of the reported `observed_addr` matches the actual remote IP of the session (`session.address`). Specifically, extract the IP from `session.address` and compare it to the IP in the reported `observed_addr` before storing it. This is the standard mitigation used by libp2p and other P2P frameworks.

### Proof of Concept
```
1. Start a victim CKB node with no public_addresses configured.
2. Connect as Peer A (any valid CKB peer identity).
3. Complete the secio handshake, then send an IdentifyMessage with:
     listen_addrs = []
     observed_addr = /ip4/1.2.3.4/tcp/9999/p2p/<victim_peer_id>
     identify = <valid identify payload>
4. While Peer A remains connected, connect as Peer B.
5. Capture the IdentifyMessage sent by the victim to Peer B.
6. Assert that listen_addrs in that message contains /ip4/1.2.3.4/tcp/9999/p2p/<victim_peer_id>.
7. Verify Peer B's peer store now contains 1.2.3.4:9999 associated with the victim's peer_id.
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L152-168)
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
```

**File:** network/src/protocols/identify/mod.rs (L211-229)
```rust
        let listen_addrs = if self.callback.register(&context, version) {
            Vec::new()
        } else {
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
```

**File:** network/src/protocols/identify/mod.rs (L231-236)
```rust
        let identify = self.callback.identify();
        let data = IdentifyMessage::new(listen_addrs, session.address.clone(), identify).encode();
        let _ = context
            .quick_send_message(data)
            .await
            .map_err(|err| error!("IdentifyProtocol quick_send_message, error: {:?}", err));
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

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
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

**File:** network/src/network.rs (L512-516)
```rust
    /// add observed address for identify protocol
    pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
        let mut pending_observed_addrs = self.observed_addrs.write();
        pending_observed_addrs.insert(session_id, addr);
    }
```
