Audit Report

## Title
Unauthenticated Inbound Peer Can Inject Arbitrary Public IP into Victim's Self-Advertised Listen Addresses — (`network/src/protocols/identify/mod.rs`)

## Summary
`process_observed` accepts any `observed_addr` from a remote peer and stores it without validating that the IP matches the session's actual remote address. The stored address is later included verbatim in `local_listen_addrs()`, which is sent to every newly-connecting peer, causing the victim to advertise an attacker-controlled address as its own listen address and poisoning peer discovery network-wide.

## Finding Description
`process_observed` (lines 152–169) passes the attacker-supplied `observed` multiaddr directly to `add_observed_addr` with no IP validation and no check that the address matches `context.session.address`: [1](#0-0) 

`IdentifyCallback::add_observed_addr` (lines 497–507) only appends the local peer-id if absent, then unconditionally calls `NetworkState::add_observed_addr`: [2](#0-1) 

`NetworkState::add_observed_addr` (lines 513–516 of `network.rs`) inserts the address into `observed_addrs` keyed by `session_id` with no further validation: [3](#0-2) 

`local_listen_addrs` (lines 458–470) appends `observed_addrs` directly to the list returned to every newly-connecting peer: [4](#0-3) 

In `connected`, the output of `local_listen_addrs()` is filtered by `is_reachable` before transmission (lines 217–228): [5](#0-4) 

`is_reachable` only rejects private/loopback/link-local ranges. Any globally-routable IP (e.g., `1.2.3.4`) passes this filter and is broadcast verbatim. **Contrast with `process_listens`** (lines 138–148), which applies `is_reachable` before storing remote peers' addresses — `process_observed` has no equivalent guard against a mismatched or attacker-chosen IP: [6](#0-5) 

The injected address is also included in the `hole_punching` protocol's `listen_addrs` broadcast (lines 187–206 of `hole_punching/mod.rs`): [7](#0-6) 

The entry is removed on disconnect (lines 814–817 of `network.rs`), but the injection window is open for the entire session duration: [8](#0-7) 

## Impact Explanation
The victim node broadcasts the injected address (with its own peer-id appended) to every peer that connects after the attacker. Receiving peers add this address to their peer stores and may attempt to connect to the attacker-controlled endpoint. Multiple simultaneous attacker sessions inject multiple addresses. This constitutes a low-cost, network-wide peer discovery poisoning attack that can redirect connection attempts across the CKB P2P network, fitting the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
Exploitation requires only a valid inbound TCP connection and a standard CKB peer handshake — no special privileges, no proof-of-work, no key material. The attacker sends a single crafted `IdentifyMessage` with an arbitrary globally-routable `observed_addr`. The attack is repeatable with multiple simultaneous sessions and persists for the full session duration.

## Recommendation
In `process_observed`, validate that the IP component of `observed_addr` matches the actual remote IP of the session (`context.session.address`). At minimum, apply the same `is_reachable` filter used in `process_listens` before storing the address. Ideally, reject any `observed_addr` whose IP does not match the session's remote IP, since a peer can only legitimately observe the address it is actually connecting from.

## Proof of Concept
```
1. Attacker establishes an inbound TCP connection to the victim node and
   completes the standard CKB peer handshake (valid network identify bytes).
2. Attacker sends IdentifyMessage with:
     observed_addr = /ip4/1.2.3.4/tcp/8114   (attacker-controlled public IP)
     listen_addrs  = []
     identify      = valid network identify bytes
3. Victim calls process_observed(/ip4/1.2.3.4/tcp/8114),
   which calls add_observed_addr, appends /p2p/<victim_peer_id>,
   and stores /ip4/1.2.3.4/tcp/8114/p2p/<victim_peer_id> in observed_addrs.
4. A second legitimate peer connects to the victim.
5. Victim's connected() calls local_listen_addrs(), which returns
   [... public_addrs ..., /ip4/1.2.3.4/tcp/8114/p2p/<victim_peer_id>].
6. is_reachable(1.2.3.4) == true, so the address passes the filter.
7. The second peer receives the injected address in the victim's
   IdentifyMessage listen_addrs field and stores it in its peer store.
8. The second peer (and peers it gossips to) attempt to connect to 1.2.3.4:8114.

Unit test plan: construct an IdentifyProtocol with a mock callback,
call received() with a crafted IdentifyMessage containing observed_addr
= /ip4/1.2.3.4/tcp/8114, then assert that local_listen_addrs() returns
an address containing 1.2.3.4 despite the session's remote IP being different.
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L138-148)
```rust
            let global_ip_only = self.global_ip_only;
            let reachable_addrs = listens
                .into_iter()
                .filter(|addr| match multiaddr_to_socketaddr(addr) {
                    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
                    None => true,
                })
                .collect::<Vec<_>>();
            self.callback
                .add_remote_listen_addrs(session, reachable_addrs);
            MisbehaveResult::Continue
```

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

**File:** network/src/protocols/identify/mod.rs (L217-228)
```rust
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

**File:** network/src/network.rs (L814-817)
```rust
                self.network_state
                    .observed_addrs
                    .write()
                    .remove(&session_context.id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L187-206)
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
                } else {
                    let iter = public_addr
                        .iter()
                        .map(Multiaddr::to_vec)
                        .map(|v| packed::Address::new_builder().bytes(v).build());
                    packed::AddressVec::new_builder().extend(iter).build()
                }
            };
```
