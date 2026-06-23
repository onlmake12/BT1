Now I have the complete picture. Let me trace every step of the attack path with exact line references.

**Key findings:**

1. `process_observed` (line 167) passes the attacker-controlled `observed_addr` directly to `add_observed_addr` with no IP validation.
2. `IdentifyCallback::add_observed_addr` (lines 497–504) only appends the local peer_id if missing — no IP ownership check.
3. `NetworkState::add_observed_addr` (lines 513–515) blindly inserts into the map keyed by session_id.
4. `local_listen_addrs` (lines 461–465) fills remaining slots from `observed_addrs` when `public_addrs < 10`.
5. In `connected` (lines 217–224), the outgoing filter is `!self.global_ip_only || is_reachable(socket_addr.ip())` — this blocks loopback/private IPs but **passes any globally routable third-party IP**.
6. On `SessionClose` (lines 814–817), the entry **is** removed — so persistence is limited to the attacker's connection window, but the attacker can hold the connection open indefinitely.

---

### Title
Unauthenticated `observed_addr` Injection Allows False Listen-Address Advertisement to Peers — (`network/src/protocols/identify/mod.rs`)

### Summary
Any peer can send a crafted `IdentifyMessage` whose `observed_addr` field contains an arbitrary globally-routable IP. The address is stored without validation and subsequently broadcast to every newly-connected peer as one of the local node's own listen addresses, as long as the node has fewer than 10 configured public addresses.

### Finding Description
When the identify protocol receives a message, `process_observed` calls `IdentifyCallback::add_observed_addr` with the raw peer-supplied `Multiaddr`: [1](#0-0) 

`add_observed_addr` only appends the local peer-id if absent; it performs no IP-ownership or reachability validation before storing: [2](#0-1) 

`NetworkState::add_observed_addr` inserts unconditionally: [3](#0-2) 

`local_listen_addrs` then fills any remaining slots (up to `MAX_RETURN_LISTEN_ADDRS = 10`) from `observed_addrs`: [4](#0-3) 

When a new peer connects, the result of `local_listen_addrs()` is filtered only by `is_reachable` — which passes any globally-routable IP — and sent in the outgoing `IdentifyMessage`: [5](#0-4) 

The entry is removed on session close, so persistence is bounded by the attacker's connection lifetime: [6](#0-5) 

### Impact Explanation
While the attacker maintains the session, every peer that subsequently connects to the victim receives the injected address as one of the victim's advertised listen addresses. This causes:
- Peers to attempt connections to a third-party IP (traffic redirection / amplification toward an arbitrary host).
- The victim's address set visible via `get_peers` RPC to contain a false entry.
- Degraded peer-discovery quality for the victim's neighbors.

The attack does not affect consensus, transaction validity, or fund security.

### Likelihood Explanation
Any peer that can complete the identify handshake (i.e., any node on the same network) can trigger this. No special privileges are required. The attacker only needs to hold the connection open to maintain the injected address. Nodes behind NAT with zero configured `public_addresses` are fully exposed because `public_addrs` returns an empty set, so `local_listen_addrs` always falls through to `observed_addrs`. [7](#0-6) 

### Recommendation
Before storing an observed address, validate that its IP component matches the actual remote socket address of the reporting session (`session.address`). Specifically, in `add_observed_addr`, extract the IP from the supplied `Multiaddr` and compare it against `session.address`'s IP; reject or ignore the address if they differ. Additionally, apply the same `is_reachable` filter at storage time (not only at broadcast time) to prevent private/loopback addresses from ever entering `observed_addrs`.

### Proof of Concept
1. Start a victim CKB node with no `public_addresses` configured.
2. Connect an attacker node; after the identify handshake, send a second `IdentifyMessage` (or craft the first) with `observed_addr = /ip4/<arbitrary_public_ip>/tcp/8115`.
3. Connect a third (observer) node to the victim.
4. Inspect the `listen_addrs` field of the `IdentifyMessage` the victim sends to the observer — it will contain the injected IP.
5. Confirm via `get_peers` RPC on the observer that the victim's advertised addresses include the injected entry.

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

**File:** network/src/protocols/identify/mod.rs (L211-232)
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

        let identify = self.callback.identify();
        let data = IdentifyMessage::new(listen_addrs, session.address.clone(), identify).encode();
```

**File:** network/src/protocols/identify/mod.rs (L347-353)
```rust
    fn listen_addrs(&self) -> Vec<Multiaddr> {
        let addrs = self.network_state.public_addrs(MAX_RETURN_LISTEN_ADDRS * 2);
        addrs
            .into_iter()
            .take(MAX_RETURN_LISTEN_ADDRS)
            .collect::<Vec<_>>()
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

**File:** network/src/protocols/identify/mod.rs (L497-505)
```rust
    fn add_observed_addr(&mut self, mut addr: Multiaddr, session_id: SessionId) -> MisbehaveResult {
        if extract_peer_id(&addr).is_none() {
            addr.push(Protocol::P2P(Cow::Borrowed(
                self.network_state.local_peer_id().as_bytes(),
            )))
        }

        self.network_state.add_observed_addr(session_id, addr);
        // NOTE: for future usage
```

**File:** network/src/network.rs (L513-516)
```rust
    pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
        let mut pending_observed_addrs = self.observed_addrs.write();
        pending_observed_addrs.insert(session_id, addr);
    }
```

**File:** network/src/network.rs (L814-817)
```rust
                self.network_state
                    .observed_addrs
                    .write()
                    .remove(&session_context.id);
```
