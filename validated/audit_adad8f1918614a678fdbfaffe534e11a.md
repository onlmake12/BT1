Based on my analysis of the code, I can trace the full exploit path concretely.

---

### Title
Unauthenticated Observed-Address Injection via Identify Protocol Poisons Network-Wide Peer Discovery — (`network/src/protocols/identify/mod.rs`)

### Summary

An unprivileged inbound peer can send a crafted `IdentifyMessage` with an arbitrary attacker-controlled `observed_addr`. The address is stored without validation into `NetworkState::observed_addrs` and subsequently included in `local_listen_addrs()`, which is broadcast to every future peer that connects. The poisoned address persists after the attacker disconnects because `unregister` never cleans up `observed_addrs`.

### Finding Description

**Step 1 — Attacker-controlled entry point.**

`IdentifyProtocol::received()` decodes any incoming `IdentifyMessage` and calls `process_observed()` with the raw `message.observed_addr`: [1](#0-0) 

**Step 2 — No validation in `process_observed()`.**

`process_observed()` passes the address directly to `add_observed_addr` and unconditionally returns `MisbehaveResult::Continue` — no IP validation, no reachability check, no comparison against the session's actual remote address: [2](#0-1) 

**Step 3 — Stored without validation in `IdentifyCallback::add_observed_addr()`.**

The only operation performed is appending the local peer_id if absent. The address is then stored into `network_state.observed_addrs` keyed by `session_id`: [3](#0-2) 

**Step 4 — Poisoned address included in `local_listen_addrs()`.**

When the node's listen address list has fewer than `MAX_RETURN_LISTEN_ADDRS` entries, `observed_addrs` are appended directly: [4](#0-3) 

**Step 5 — Broadcast to every subsequent peer on connect.**

In `connected()`, `local_listen_addrs()` (which now includes the poisoned address) is filtered only by `global_ip_only` / `is_reachable`. A globally-routable attacker-controlled IP passes this filter and is sent to the new peer: [5](#0-4) 

**Step 6 — Poisoned address persists after disconnect.**

`IdentifyCallback::unregister()` only updates the peer store for outbound sessions. It never removes the entry from `observed_addrs`, so the poisoned address survives the attacker's disconnection: [6](#0-5) 

**Critical asymmetry:** The `global_ip_only` / `is_reachable` filter is applied to `listen_addrs` in `process_listens()` at ingestion time, but observed addresses bypass this filter entirely at ingestion — they are only filtered at broadcast time. An attacker using a globally-routable IP (e.g., their own VPS) passes even the broadcast-time filter.

### Impact Explanation

Every honest node that subsequently connects to the victim receives the attacker-controlled address as part of the victim's advertised listen addresses. Those nodes store it in their peer stores (via `add_remote_listen_addrs` → `peer_store.add_addr`) and may propagate it further via the Discovery protocol. Over time this degrades peer discovery quality across the network, enabling targeted network partitioning by directing nodes toward attacker-controlled endpoints.

### Likelihood Explanation

- Requires only an inbound TCP connection — no authentication, no stake, no PoW.
- The identify protocol runs on every peer connection by default.
- A single attacker with one VPS IP can poison the observed_addrs of any reachable node.
- The effect is persistent (survives disconnect) and self-amplifying (propagates to downstream peers).

### Recommendation

1. **Validate observed_addr at ingestion**: In `process_observed()`, verify that the observed address's IP matches (or is consistent with) the session's actual remote address (`session.address`). Reject or ignore addresses that do not correspond to the session's source IP.
2. **Apply `is_reachable` filter at ingestion** in `add_observed_addr`, not only at broadcast time.
3. **Clean up on disconnect**: In `unregister()` (or `disconnected()`), remove the session's entry from `observed_addrs`.
4. **Cap observed_addrs**: Limit the total number of entries to prevent unbounded growth.

### Proof of Concept

```
1. Attacker connects to victim node as inbound peer (standard TCP dial).
2. Attacker sends a valid IdentifyMessage (correct network name/flags) with:
     observed_addr = /ip4/<attacker-VPS-IP>/tcp/8115
3. Victim stores /ip4/<attacker-VPS-IP>/tcp/8115/p2p/<victim-peer-id>
   in network_state.observed_addrs[attacker_session_id].
4. Attacker disconnects.
5. Honest peer B connects to victim.
6. Victim's connected() calls local_listen_addrs(), which appends observed_addrs.
7. /ip4/<attacker-VPS-IP>/tcp/8115/p2p/<victim-peer-id> passes is_reachable() check.
8. Peer B receives it as a listen address of the victim, stores it in its peer store,
   and may propagate it to further peers via Discovery.
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

**File:** network/src/protocols/identify/mod.rs (L211-236)
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
        let _ = context
            .quick_send_message(data)
            .await
            .map_err(|err| error!("IdentifyProtocol quick_send_message, error: {:?}", err));
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

**File:** network/src/protocols/identify/mod.rs (L368-378)
```rust
    fn unregister(&self, context: &ProtocolContextMutRef) {
        if context.session.ty.is_outbound() {
            // Due to the filtering strategy of the peer store, if the node is
            // disconnected after a long connection is maintained for more than seven days,
            // it is possible that the node will be accidentally evicted, so it is necessary
            // to reset the last_connected_time of the node when disconnected.
            self.network_state.with_peer_store_mut(|peer_store| {
                peer_store.update_outbound_addr_last_connected_ms(context.session.address.clone());
            });
        }
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
