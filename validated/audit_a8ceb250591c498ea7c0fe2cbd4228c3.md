Audit Report

## Title
Attacker-Controlled `observed_addr` Injected into Victim's Advertised `listen_addrs` via Identify Protocol — (`network/src/protocols/identify/mod.rs`)

## Summary
Any peer that opens an inbound TCP connection to a victim node can inject arbitrary globally-routable addresses into the victim's `observed_addrs` map. For NAT nodes (empty `public_addrs`), `local_listen_addrs()` fills all advertised slots from `observed_addrs`, causing the victim to re-advertise up to 10 fully attacker-controlled addresses as its own listen addresses to every subsequent outbound peer. These propagate network-wide via Discovery, making the victim unreachable.

## Finding Description

**Step 1 — `process_observed` performs no validation.**
`process_observed` (lines 152–169) passes the attacker-supplied `observed` address directly to `add_observed_addr` with no check that the IP matches `context.session.address`. [1](#0-0) 

**Step 2 — `add_observed_addr` appends local peer ID and stores unconditionally.**
The only operation is appending the victim's own peer ID if absent, then inserting into the map — no content or source validation. [2](#0-1) 

**Step 3 — `NetworkState::add_observed_addr` has no content guard.**
The backing store is `RwLock<HashMap<PeerIndex, Multiaddr>>` — one slot per active session, bounded only by `max_inbound_peers`. [3](#0-2) [4](#0-3) 

**Step 4 — `local_listen_addrs()` fills remaining slots from `observed_addrs`.**
`listen_addrs()` draws from `public_addrs`, which is empty for NAT nodes (private IPs filtered at init). When fewer than `MAX_RETURN_LISTEN_ADDRS = 10` public addresses exist, attacker-controlled entries fill the gap. [5](#0-4) [6](#0-5) 

**Step 5 — Poisoned addresses are broadcast to every new outbound peer.**
In `connected()`, `local_listen_addrs()` output is filtered only by `is_reachable` — trivially satisfied by any globally-routable IP — then sent in the `IdentifyMessage`. [7](#0-6) 

**Step 6 — Entries persist until session close.**
The attacker holds connections open; entries are only removed on session disconnect. [8](#0-7) 

**Why existing checks fail:** The `is_reachable` filter in `connected()` is the only guard and is trivially bypassed by any public IP (e.g., `8.8.8.8`). There is no check that `observed_addr` matches the actual remote IP of the session that sent it. `public_addrs` initialization correctly filters private IPs but leaves `public_addrs` empty for NAT nodes, making the `observed_addrs` fallback the sole source of advertised addresses. [9](#0-8) 

## Impact Explanation

**High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A targeted victim node advertises attacker-controlled addresses as its own listen addresses to every outbound peer. Receiving peers store these in their peer stores and re-announce them via Discovery, causing network-wide propagation of poisoned addresses. Nodes attempting to reach the victim via these addresses fail, severing the victim's inbound connectivity. Applied at scale across multiple nodes, this degrades the reachability graph of the CKB P2P network and can facilitate eclipse attack preconditions.

## Likelihood Explanation

- Requires only the ability to open inbound TCP connections — no authentication, no PoW, no privileged role.
- The `is_reachable` filter is trivially bypassed with any globally-routable IP.
- NAT nodes (the majority of deployments) have an empty `public_addrs`, making all 10 advertised slots available for attacker injection.
- The attacker sustains the attack by keeping connections open within `max_inbound_peers`.
- The attack is repeatable and requires no special timing or race conditions.

## Recommendation

1. **Validate `observed_addr` against the session's actual remote IP.** In `process_observed`, reject any `observed_addr` whose IP component does not match `context.session.address`'s IP. A peer can only truthfully report what it actually observed.
2. **Do not include unverified `observed_addrs` in outgoing `listen_addrs`.** `observed_addrs` should be used only for local NAT self-detection, not re-advertised to third parties.
3. **Require corroboration from multiple independent peers** before promoting an `observed_addr` to the advertised address set, similar to libp2p's external address confirmation mechanism.

## Proof of Concept

```
1. Victim node has no `public_addresses` configured (NAT node — common deployment).
2. Attacker opens N inbound connections to victim (N ≤ max_inbound_peers).
3. Each connection sends IdentifyMessage {
       observed_addr: /ip4/<attacker_chosen_ip_N>/tcp/8115,
       ...
   }
4. NetworkState::observed_addrs now contains N attacker-controlled entries.
5. Victim dials any new outbound peer → connected() fires →
   local_listen_addrs() returns up to 10 attacker-controlled addresses
   (public_addrs is empty, all slots come from observed_addrs).
6. New peer receives IdentifyMessage {
       listen_addrs: [/ip4/attacker_ip_1/tcp/8115/p2p/<victim_id>, ...]
   }
7. New peer stores these in its peer store and announces them via Discovery.
8. Assert: listen_addrs in the captured IdentifyMessage contains none of
   the victim's real addresses.
```

To reproduce: instrument `connected()` to log the outgoing `IdentifyMessage`, run a victim node behind NAT, connect with a modified peer sending crafted `observed_addr` values, then observe a third peer's received `IdentifyMessage` from the victim.

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

**File:** network/src/network.rs (L82-82)
```rust
    observed_addrs: RwLock<HashMap<PeerIndex, Multiaddr>>,
```

**File:** network/src/network.rs (L102-121)
```rust
        let public_addrs: HashSet<Multiaddr> = config
            .listen_addresses
            .iter()
            .chain(config.public_addresses.iter())
            .cloned()
            .filter_map(|mut addr| match multiaddr_to_socketaddr(&addr) {
                Some(socket_addr) if !is_reachable(socket_addr.ip()) => None,
                _ => {
                    match extract_peer_id(&addr) {
                        Some(peer_id) if peer_id != local_peer_id => {
                            error!("Don't include addresses that not associated with this node in the public_addresses list: {:?}", addr);
                            std::process::exit(1);
                        }
                        Some(_) => (),
                        None => addr.push(Protocol::P2P(Cow::Borrowed(local_peer_id.as_bytes()))),
                    }
                    Some(addr)
                }
            })
            .collect();
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
