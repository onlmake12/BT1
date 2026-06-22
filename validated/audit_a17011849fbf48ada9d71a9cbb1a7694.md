### Title
Unvalidated `listen_addrs` in Identify Protocol Enables Peer Store Poisoning via Address Injection — (`File: network/src/protocols/identify/mod.rs`)

---

### Summary

The Identify protocol validates the outer `identify` field of a peer's handshake message (network/chain identity check) but performs no origin validation on the nested `listen_addrs` field. Any inbound peer can inject up to 10 arbitrary globally-reachable IP addresses into the local peer store, which are then propagated network-wide via the Discovery protocol. This is a direct structural analog to the reported open-redirect: the outer container is validated, but a secondary embedded value is used without checking it against the trusted source.

---

### Finding Description

When a peer connects and sends an `IdentifyMessage`, the node processes three fields in sequence inside `received()`:

1. **`identify`** — validated via `self.identify.verify(identify)`. If the network/chain identity does not match, the peer is banned. This is the "outer" validation.
2. **`listen_addrs`** — passed to `process_listens()`, which only checks count (`≤ MAX_ADDRS = 10`) and global IP reachability (`is_reachable()`). **No check is performed that any address in this list matches the peer's actual connection IP.**
3. **`observed_addr`** — passed to `process_observed()`. [1](#0-0) 

Inside `process_listens`, the filter is:

```rust
let reachable_addrs = listens
    .into_iter()
    .filter(|addr| match multiaddr_to_socketaddr(addr) {
        Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
        None => true,   // onion addresses pass unconditionally
    })
    .collect::<Vec<_>>();
self.callback.add_remote_listen_addrs(session, reachable_addrs);
``` [2](#0-1) 

`add_remote_listen_addrs` then writes every surviving address directly into the peer store:

```rust
self.network_state.with_peer_store_mut(|peer_store| {
    for addr in addrs {
        if let Err(err) = peer_store.add_addr(addr.clone(), flags) { ... }
    }
})
``` [3](#0-2) 

The code itself acknowledges the asymmetry. For inbound sessions, `received_identify` deliberately does **not** add the actual connection address to the peer store, with the comment:

> *"because inbound address can't feeler during staying connected and if set it to peer store, it will be broadcast to the entire network, but this is an unverified address"* [4](#0-3) 

Yet `process_listens` runs unconditionally for **all** sessions (inbound and outbound), and the peer-supplied `listen_addrs` — which are even less verified than the actual connection address — are committed to the peer store without any origin check.

The `add_addr` path in the peer store stores these with `last_connected_at_ms = 0` (unverified) and default score, making them eligible for broadcast via the Discovery protocol's `get_random` → `Nodes` message path. [5](#0-4) 

---

### Impact Explanation

An attacker who makes a single inbound connection can inject up to 10 arbitrary globally-reachable addresses per connection into the victim node's peer store. Those addresses are then:

- Stored and returned by `fetch_random_addrs` in the peer store.
- Broadcast to every peer that sends a `GetNodes` request via the Discovery protocol's `Nodes` response. [6](#0-5) 

This enables:

1. **Network-wide address poisoning**: Injected addresses propagate transitively across the entire P2P network, not just the directly connected node.
2. **Eclipse attack amplification**: An attacker can pre-populate the peer stores of many nodes with attacker-controlled addresses, increasing the probability that victim nodes connect exclusively to attacker peers.
3. **Connection resource waste**: Injecting large numbers of unreachable addresses causes nodes to waste outbound connection slots and retry budgets.
4. **Onion address injection**: The `None => true` branch in `process_listens` means onion addresses bypass even the reachability filter, allowing injection of arbitrary `.onion` addresses with zero validation.

---

### Likelihood Explanation

- **No authentication required**: Any peer can open an inbound connection to a CKB node. The Identify protocol runs on every new session.
- **Low cost**: One TCP connection is sufficient to inject 10 addresses. The attacker can open many connections in parallel.
- **Propagation multiplier**: Each injected address is re-broadcast to all peers that query the victim node's Discovery protocol, so the effective reach is far larger than the single connection.
- **Persistent effect**: Addresses remain in the peer store until `ADDR_MAX_RETRIES` failed connection attempts are exhausted, giving the injected entries a window of propagation.

---

### Recommendation

In `process_listens`, validate that each address in `listen_addrs` shares the same IP as the peer's actual connection address (`session.address`). Specifically, extract the IP from `session.address` and reject any listen address whose IP does not match. For onion addresses, only accept them if the peer's actual connection is also an onion session. This mirrors the fix described in the reference report: parse the secondary field and check its origin against the trusted source before use.

---

### Proof of Concept

```
1. Attacker opens a TCP connection to a CKB node's P2P port.
2. Attacker completes the tentacle handshake (no key required; any peer can connect).
3. Attacker sends a valid IdentifyMessage with:
     - identify: correct network magic bytes (publicly known per chain spec)
     - listen_addrs: [
         /ip4/1.2.3.4/tcp/8115,   ← attacker-controlled node A
         /ip4/5.6.7.8/tcp/8115,   ← attacker-controlled node B
         ... (up to 10 entries)
       ]
4. process_listens() accepts all entries (count ≤ 10, IPs are globally reachable).
5. add_remote_listen_addrs() writes all 10 addresses into the peer store.
6. Any peer that subsequently sends GetNodes receives these addresses in the
   Nodes response and adds them to its own peer store.
7. Repeat from step 1 with new connections to saturate the peer store.
``` [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** network/src/protocols/identify/mod.rs (L123-150)
```rust
    fn process_listens(
        &mut self,
        context: &mut ProtocolContextMutRef,
        listens: Vec<Multiaddr>,
    ) -> MisbehaveResult {
        let session = context.session;
        let info = self
            .remote_infos
            .get_mut(&session.id)
            .expect("RemoteInfo must exists");

        if listens.len() > MAX_ADDRS {
            self.callback
                .misbehave(&info.session, Misbehavior::TooManyAddresses(listens.len()))
        } else {
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
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L268-289)
```rust
                if let MisbehaveResult::Disconnect = self
                    .callback
                    .received_identify(&mut context, message.identify)
                    .await
                {
                    error!(
                        "Disconnect IdentifyProtocol session {:?} due to invalid identify message.",
                        session,
                    );
                    let _ = context.disconnect(session.id).await;
                    return;
                }
                if let MisbehaveResult::Disconnect =
                    self.process_listens(&mut context, message.listen_addrs.clone())
                {
                    error!(
                        "Disconnect IdentifyProtocol session {:?} due to invalid listen addrs: {:?}.",
                        session, message.listen_addrs,
                    );
                    let _ = context.disconnect(session.id).await;
                    return;
                }
```

**File:** network/src/protocols/identify/mod.rs (L415-423)
```rust
                if context.session.ty.is_outbound() {
                    // why don't set inbound here?
                    // because inbound address can't feeler during staying connected
                    // and if set it to peer store, it will be broadcast to the entire network,
                    // but this is an unverified address

                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
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

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
```

**File:** network/src/protocols/discovery/mod.rs (L170-205)
```rust
                    DiscoveryMessage::Nodes(nodes) => {
                        if let Some(misbehavior) = verify_nodes_message(&nodes)
                            && check(misbehavior)
                        {
                            if context.disconnect(session.id).await.is_err() {
                                debug!("Disconnect {:?} msg failed to send", session.id)
                            }
                            return;
                        }

                        if let Some(state) = self.sessions.get_mut(&session.id) {
                            if !nodes.announce && state.received_nodes {
                                warn!("Nodes (announce=false) message received");
                                if check(Misbehavior::DuplicateFirstNodes)
                                    && context.disconnect(session.id).await.is_err()
                                {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                            } else {
                                let addrs = nodes
                                    .items
                                    .into_iter()
                                    .flat_map(|node| {
                                        node.addresses.into_iter().map(move |a| (a, node.flags))
                                    })
                                    .collect::<Vec<_>>();

                                state.addr_known.extend(addrs.iter());
                                // Non-announce nodes can only receive once
                                // Due to the uncertainty of the other party’s state,
                                // the announce node may be sent out first, and it must be
                                // determined to be Non-announce before the state can be changed
                                if !nodes.announce {
                                    state.received_nodes = true;
                                }
                                self.addr_mgr.add_new_addrs(session.id, addrs);
```

**File:** network/src/protocols/discovery/mod.rs (L347-363)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
        }
    }
```
