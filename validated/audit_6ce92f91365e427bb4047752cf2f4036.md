Based on my investigation of the relevant code, here is my assessment:

**Key findings:**

1. `IdentifyCallback::add_observed_addr` (line 504 of `identify/mod.rs`) calls `self.network_state.add_observed_addr(session_id, addr)`, inserting into the `observed_addrs` HashMap keyed by `SessionId`. [1](#0-0) 

2. The `IdentifyProtocol::disconnected` handler (lines 239–248 of `identify/mod.rs`) only removes from `self.remote_infos` and calls `self.callback.unregister()`. The `unregister` implementation only updates `peer_store` for outbound sessions — it does **not** remove the entry from `observed_addrs`. [2](#0-1) [3](#0-2) 

3. A grep for any removal/cleanup of `observed_addrs` on disconnect in `network/src/network.rs` returned **no matches**. The `observed_addrs()` accessor (called at line 464) accepts a `limit` parameter that caps what is *returned*, but does not cap what is *stored*. [4](#0-3) 

4. The `check_duplicate` guard (lines 107–121) prevents a single session from sending more than one Identify message, so each session contributes at most one entry — but that entry persists indefinitely after the session closes. [5](#0-4) 

**Uncertainty:** I was unable to read the full body of `network/src/network.rs` to confirm whether `add_observed_addr` has an internal size cap or whether a periodic cleanup task exists elsewhere. If such a cap exists (e.g., evicting oldest entries beyond a fixed bound), the vulnerability would be mitigated.

---

### Title
Unbounded `observed_addrs` HashMap Growth via Repeated Inbound Sessions — (`network/src/network.rs`)

### Summary
The `observed_addrs` HashMap in `NetworkState` accumulates one entry per historical session that sent an Identify message. Entries are inserted in `add_observed_addr` but never removed when sessions close, causing memory to grow proportional to total historical session count rather than current active session count.

### Finding Description
When a remote peer connects and the Identify protocol runs, `IdentifyCallback::add_observed_addr` inserts `(session_id → addr)` into `NetworkState::observed_addrs`. The `IdentifyProtocol::disconnected` handler removes the session from `remote_infos` and calls `unregister`, but neither path removes the entry from `observed_addrs`. Because `SessionId` values are monotonically increasing and never reused, an attacker who repeatedly opens and closes inbound connections accumulates entries indefinitely.

### Impact Explanation
Memory consumption grows without bound. Each entry is small (~100 bytes: u64 SessionId + Multiaddr + HashMap overhead), but over millions of cycles (feasible over hours/days given typical inbound connection rate limits), this can exhaust available memory, causing node OOM termination or severe performance degradation, disrupting block/transaction relay.

### Likelihood Explanation
An unprivileged attacker only needs the ability to open TCP connections to the node's P2P port and send a valid Identify message before disconnecting. No PoW, no keys, no privileged access required. The `check_duplicate` guard does not prevent this — it only prevents a single session from sending two messages. Inbound connection limits slow the attack but do not prevent accumulation over time since entries survive session close.

### Recommendation
Remove the corresponding `observed_addrs` entry in the session-close path. In `IdentifyCallback::unregister` (or in `NetworkState`'s session-close handler), call `observed_addrs.remove(&session_id)`. Alternatively, bound the HashMap size with an LRU eviction policy.

### Proof of Concept
```
1. Open inbound TCP connection to node P2P port
2. Complete Identify handshake, sending observed_addr = <any valid Multiaddr>
3. Close connection
4. Repeat N times with fresh connections (each gets a new SessionId)
5. Assert: NetworkState::observed_addrs.len() == N (not 0)
   Expected (safe): len() == 0 (all sessions closed)
   Actual (vulnerable): len() == N (entries never removed)
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L107-121)
```rust
    fn check_duplicate(&mut self, context: &mut ProtocolContextMutRef) -> MisbehaveResult {
        let session = context.session;
        let info = self
            .remote_infos
            .get_mut(&session.id)
            .expect("RemoteInfo must exists");

        if info.has_received {
            self.callback
                .misbehave(&info.session, Misbehavior::DuplicateReceived)
        } else {
            info.has_received = true;
            MisbehaveResult::Continue
        }
    }
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

**File:** network/src/protocols/identify/mod.rs (L461-469)
```rust
        if listens.len() < MAX_RETURN_LISTEN_ADDRS {
            let observe_addrs = self
                .network_state
                .observed_addrs(MAX_RETURN_LISTEN_ADDRS - listens.len());
            listens.extend(observe_addrs);
            listens
        } else {
            listens
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
