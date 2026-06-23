### Title
Unvalidated `observed_addr` Injection Poisons Node's Own Address Advertisement - (File: `network/src/protocols/identify/mod.rs`)

---

### Summary

Any connected P2P peer can inject an arbitrary `Multiaddr` into a victim node's `observed_addrs` store via the Identify protocol. Because `observed_addrs` are unconditionally included in the node's own `listen_addrs` when it sends `IdentifyMessage` to subsequent peers, the attacker-controlled address propagates network-wide as if it were the victim node's legitimate listen address.

---

### Finding Description

The Identify protocol's `received` handler in `IdentifyProtocol` processes three fields from the remote peer's message: `identify`, `listen_addrs`, and `observed_addr`. The `observed_addr` field is semantically "the address I see you connecting from" — it is used for NAT traversal so a node behind NAT can learn its external address.

The handler calls `process_observed`, which calls `IdentifyCallback::add_observed_addr` with no validation:

```rust
// network/src/protocols/identify/mod.rs  line 152-168
fn process_observed(
    &mut self,
    context: &mut ProtocolContextMutRef,
    observed: Multiaddr,
) -> MisbehaveResult {
    ...
    self.callback.add_observed_addr(observed, info.session.id);
    MisbehaveResult::Continue
}
```

`IdentifyCallback::add_observed_addr` appends the local node's peer-id if absent, then stores the address unconditionally:

```rust
// network/src/protocols/identify/mod.rs  line 497-507
fn add_observed_addr(&mut self, mut addr: Multiaddr, session_id: SessionId) -> MisbehaveResult {
    if extract_peer_id(&addr).is_none() {
        addr.push(Protocol::P2P(Cow::Borrowed(
            self.network_state.local_peer_id().as_bytes(),
        )))
    }
    self.network_state.add_observed_addr(session_id, addr);
    MisbehaveResult::Continue
}
```

`NetworkState::add_observed_addr` writes directly into the shared `observed_addrs` map:

```rust
// network/src/network.rs  line 512-516
pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
    let mut pending_observed_addrs = self.observed_addrs.write();
    pending_observed_addrs.insert(session_id, addr);
}
```

These stored addresses are then included in the node's own `listen_addrs` when it connects to any subsequent peer:

```rust
// network/src/protocols/identify/mod.rs  line 457-470
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

`local_listen_addrs` is called inside `IdentifyProtocol::connected` and the result is sent as the node's own `IdentifyMessage` to every new peer:

```rust
// network/src/protocols/identify/mod.rs  line 206-236
async fn connected(&mut self, context: ProtocolContextMutRef<'_>, version: &str) {
    ...
    let listen_addrs = ... self.callback.local_listen_addrs() ...
    let data = IdentifyMessage::new(listen_addrs, session.address.clone(), identify).encode();
    let _ = context.quick_send_message(data).await ...
}
```

The same `observed_addrs` are also included in hole-punching `ConnectionRequest` and `ConnectionRequestDelivered` messages broadcast to multiple peers simultaneously:

```rust
// network/src/protocols/hole_punching/mod.rs  line 187-198
let observed_addrs = self.network_state.observed_addrs(ADDRS_COUNT_LIMIT - public_addr.len());
let iter = public_addr.iter().chain(observed_addrs.iter()) ...
```

There is **no check** that the `observed_addr` supplied by the remote peer actually matches the session's transport-layer source address. Any syntactically valid `Multiaddr` is accepted and stored.

---

### Impact Explanation

An attacker who establishes a single inbound or outbound P2P connection to a victim node can:

1. **Inject an arbitrary address** (e.g., `attacker.com:8114/p2p/<victim-peer-id>`) into the victim's `observed_addrs`.
2. The victim node then **advertises this address as its own** to every subsequent peer it connects to via the Identify protocol.
3. Peers receiving the poisoned `IdentifyMessage` call `add_remote_listen_addrs`, which writes the attacker's address into their peer stores.
4. Those peers may **dial the attacker's address** believing it is the victim node, enabling connection hijacking or denial of service.
5. The poisoned address also propagates through the **hole-punching protocol** via gossip broadcast, amplifying the reach.

The victim node's `observed_addrs` map is keyed by `SessionId`, so a single session can overwrite the entry for that session at will, and the entry persists until the session closes.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged peer that can establish a TCP connection to the victim node (inbound) or that the victim dials (outbound) can send a crafted `IdentifyMessage`. No authentication or special privilege is required beyond a valid P2P handshake.
- **Ease**: The attacker only needs to connect once and send one message. The `IdentifyMessage` is sent immediately on protocol open, so the window is the entire session lifetime.
- **Persistence**: The poisoned address remains in `observed_addrs` for the duration of the session and is broadcast to every new peer the victim connects to during that time.
- **Amplification**: Via hole-punching gossip, the address can reach `sqrt(total_connections)` peers per notify interval.

---

### Recommendation

- **Short term**: In `process_observed` / `add_observed_addr`, validate that the `observed_addr`'s IP component matches the transport-layer source IP of the session (`context.session.address`). Reject or ignore addresses whose IP does not match the session's actual remote IP.
- **Long term**: Treat `observed_addrs` as hints only and require corroboration from multiple independent peers before including an observed address in outbound `listen_addrs`. This is the approach used by Bitcoin Core's address-manager and libp2p's identify-push extension.

---

### Proof of Concept

1. Attacker Eve establishes a TCP connection to victim node V (inbound or outbound).
2. After the secio handshake, Eve sends an `IdentifyMessage` with:
   - `observed_addr` = `/ip4/1.2.3.4/tcp/8114/p2p/<V's peer-id>` (attacker-controlled IP)
   - `listen_addrs` = any valid addresses
   - `identify` = valid network identifier bytes
3. V's `IdentifyProtocol::received` calls `process_observed` → `add_observed_addr` → `NetworkState::add_observed_addr`, storing `1.2.3.4:8114` in `observed_addrs[eve_session_id]`.
4. V subsequently connects to peer P. `IdentifyProtocol::connected` calls `local_listen_addrs`, which appends `1.2.3.4:8114` to V's own listen addresses and sends them to P.
5. P calls `add_remote_listen_addrs`, writing `1.2.3.4:8114` into its peer store under V's peer-id.
6. P later dials `1.2.3.4:8114` expecting to reach V, but reaches Eve's server instead.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** network/src/protocols/identify/mod.rs (L206-236)
```rust
    async fn connected(&mut self, context: ProtocolContextMutRef<'_>, version: &str) {
        let session = context.session;
        debug!("IdentifyProtocol connected, session: {:?}", session);
        let remote_info = RemoteInfo::new(session.clone(), Duration::from_secs(DEFAULT_TIMEOUT));
        self.remote_infos.insert(session.id, remote_info);
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
        let _ = context
            .quick_send_message(data)
            .await
            .map_err(|err| error!("IdentifyProtocol quick_send_message, error: {:?}", err));
```

**File:** network/src/protocols/identify/mod.rs (L457-470)
```rust
    /// Get local listen addresses
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

**File:** network/src/protocols/identify/mod.rs (L472-495)
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

**File:** network/src/network.rs (L512-516)
```rust
    /// add observed address for identify protocol
    pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
        let mut pending_observed_addrs = self.observed_addrs.write();
        pending_observed_addrs.insert(session_id, addr);
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L187-198)
```rust
            let listen_addrs = {
                let public_addr = self.network_state.public_addrs(ADDRS_COUNT_LIMIT);
                if public_addr.len() < ADDRS_COUNT_LIMIT {
                    let observed_addrs = self
                        .network_state
                        .observed_addrs(ADDRS_COUNT_LIMIT - public_addr.len());
                    let iter = public_addr
                        .iter()
                        .chain(observed_addrs.iter())
                        .map(Multiaddr::to_vec)
                        .map(|v| packed::Address::new_builder().bytes(v).build());
                    packed::AddressVec::new_builder().extend(iter).build()
```
