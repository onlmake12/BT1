All seven code references have been verified against the actual source. Here is the validation result:

---

Audit Report

## Title
Unbounded `observed_addr` Stored and Re-broadcast in Outgoing Identify Messages — (`network/src/protocols/identify/mod.rs`, `network/src/network.rs`)

## Summary
Any unprivileged remote peer can send an identify message containing an arbitrarily large `observed_addr`. The victim node stores it without any byte-length validation and subsequently includes it verbatim in the `listen_addrs` field of every outgoing identify message sent to new peers. This produces unbounded bandwidth amplification proportional to the victim's new-connection rate, achievable at near-zero cost to the attacker.

## Finding Description

**Step 1 — Inbound decode, no size check.**
`IdentifyMessage::decode` in `protocol.rs` calls `Multiaddr::try_from(reader.observed_addr().bytes().raw_data().to_vec()).ok()?` with no length limit. Any syntactically valid Multiaddr of arbitrary size is accepted. [1](#0-0) 

**Step 2 — `process_observed` unconditionally continues.**
`process_observed` passes the raw addr directly to `add_observed_addr` and always returns `MisbehaveResult::Continue`, regardless of addr size. [2](#0-1) 

**Step 3 — `IdentifyCallback::add_observed_addr` has no size guard.**
The callback appends a `/p2p/<local_peer_id>` component if absent, then calls `network_state.add_observed_addr` — no byte-length check anywhere. [3](#0-2) 

**Step 4 — `NetworkState::add_observed_addr` stores the raw addr unconditionally.**
The HashMap insert is a plain `insert(session_id, addr)` with no validation. [4](#0-3) 

**Step 5 — `local_listen_addrs` feeds observed addrs into outgoing messages.**
When the victim has fewer than `MAX_RETURN_LISTEN_ADDRS` (10) public addresses, `local_listen_addrs` fills the remainder from `observed_addrs`, up to 9 attacker-controlled entries. [5](#0-4) 

**Step 6 — `connected()` applies a count cap but no byte-size cap.**
The filter passes any addr whose first IP component is a reachable global IP. `.take(MAX_ADDRS)` caps the *count* at 10 but places no bound on the *byte size* of each addr. The resulting `IdentifyMessage` is serialized and sent via `quick_send_message` to every new peer. [6](#0-5) 

**Step 7 — `observed_addrs` returns stored addrs without any size filtering.**
`NetworkState::observed_addrs` reads values directly from the HashMap and returns them as-is. [7](#0-6) 

**Additional finding — `observed_addrs` entries are never evicted on disconnect.**
`disconnected()` removes from `remote_infos` and calls `unregister`, but `unregister` only updates the peer store for outbound sessions — it never removes the entry from `observed_addrs`. Large attacker-injected addrs persist in the HashMap indefinitely, even after the attacker disconnects. [8](#0-7) [9](#0-8) 

## Impact Explanation
**High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With 9 simultaneous attacker sessions (each with a different `session_id`), up to 9 × 64 KB ≈ 576 KB can be embedded in a single outgoing identify message. Every subsequent inbound or outbound connection the victim makes triggers `connected()`, which re-serializes and re-sends the bloated message. The victim's outbound bandwidth scales as `(large_addr_size) × (new_connections_per_second)` with no rate limit or message-size cap. Because entries are never evicted from `observed_addrs` on disconnect, the attacker does not even need to maintain open sessions after injection. If multiple nodes are targeted simultaneously, this produces network-wide congestion.

## Likelihood Explanation
The path is fully reachable by any unprivileged peer on the CKB P2P network. No PoW, no key, and no special role is required — only a valid TCP connection and a well-formed (but oversized) Multiaddr. The Multiaddr format imposes no maximum length, and CKB adds no such constraint. The attack is persistent: once injected, the large addr survives session teardown because `observed_addrs` is never cleaned up on disconnect.

## Recommendation
1. **Enforce a maximum byte length on `observed_addr` at decode time** in `IdentifyMessage::decode` (e.g., reject any addr whose raw byte length exceeds 256 bytes).
2. **Add a size guard in `IdentifyCallback::add_observed_addr`** before calling `network_state.add_observed_addr`.
3. **Enforce a per-address byte-length cap in `connected()`** when building `listen_addrs`, in addition to the existing count cap via `.take(MAX_ADDRS)`.
4. **Evict `observed_addrs` entries on session disconnect** inside `unregister` or `disconnected()`.
5. **Enforce a total outgoing message size cap** before calling `quick_send_message`.

## Proof of Concept
```rust
// State test sketch
let large_hostname = "a".repeat(65_000);
let large_addr: Multiaddr = format!(
    "/ip4/8.8.8.8/tcp/8115/dns4/{}/tcp/80", large_hostname
).parse().unwrap();

// Attacker sends identify message with this observed_addr to victim node.
// After processing:
//   observed_addrs[attacker_session_id] = large_addr  (64 KB+)
//
// Victim then connects to a new peer; connected() fires:
//   local_listen_addrs() returns [victim_public_addr, large_addr]
//   filter passes (8.8.8.8 is reachable)
//   take(10) passes (count = 2 ≤ 10)
//   IdentifyMessage::encode() serializes large_addr verbatim
//   quick_send_message sends ~64 KB to the new peer
//
// Attacker disconnects — large_addr remains in observed_addrs HashMap
// (unregister() does not remove it)
//
// Assert: outgoing identify message size > 60_000 bytes
// (violates the invariant that message size must be bounded
//  independently of peer input)
```

### Citations

**File:** network/src/protocols/identify/protocol.rs (L69-70)
```rust
        let observed_addr =
            Multiaddr::try_from(reader.observed_addr().bytes().raw_data().to_vec()).ok()?;
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

**File:** network/src/protocols/identify/mod.rs (L214-236)
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
        let _ = context
            .quick_send_message(data)
            .await
            .map_err(|err| error!("IdentifyProtocol quick_send_message, error: {:?}", err));
```

**File:** network/src/protocols/identify/mod.rs (L239-248)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.remote_infos
            .remove(&context.session.id)
            .expect("RemoteInfo must exists");
        debug!(
            "IdentifyProtocol disconnected, session: {:?}",
            context.session
        );
        self.callback.unregister(&context);
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

**File:** network/src/network.rs (L513-516)
```rust
    pub(crate) fn add_observed_addr(&self, session_id: SessionId, addr: Multiaddr) {
        let mut pending_observed_addrs = self.observed_addrs.write();
        pending_observed_addrs.insert(session_id, addr);
    }
```

**File:** network/src/network.rs (L519-533)
```rust
    pub(crate) fn observed_addrs(&self, count: usize) -> Vec<Multiaddr> {
        let observed_addrs = self
            .observed_addrs
            .read()
            .values()
            .cloned()
            .collect::<HashSet<_>>();
        if observed_addrs.len() <= count {
            return observed_addrs.into_iter().collect();
        } else {
            observed_addrs
                .into_iter()
                .choose_multiple(&mut rand::thread_rng(), count)
        }
    }
```
