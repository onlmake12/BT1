Audit Report

## Title
Unvalidated `observed_addr` Injection Poisons Victim Node's Advertised Listen Addresses — (File: `network/src/protocols/identify/mod.rs`)

## Summary
Any remote peer can send an identify message with an arbitrary globally-routable `observed_addr`. Because no ownership or reachability validation is performed at insertion time, the victim stores the attacker-controlled address and immediately broadcasts it as one of its own listen addresses to every subsequent connecting peer, violating the invariant that a node must only advertise its own verified listen addresses and enabling cascading address-table pollution across the CKB P2P network.

## Finding Description
**Step 1 — `process_observed` accepts any address without validation.**
`process_observed` (lines 152–169) takes the raw `observed` `Multiaddr` from the incoming identify message and immediately calls `self.callback.add_observed_addr(observed, info.session.id)`. There is no check that the address matches `session.address`, no IP reachability check, no size limit, and the function unconditionally returns `MisbehaveResult::Continue`. [1](#0-0) 

**Step 2 — `IdentifyCallback::add_observed_addr` stores the address verbatim.**
Lines 497–507: the only transformation is appending the local peer ID if absent. No content validation is performed. Always returns `MisbehaveResult::Continue`. [2](#0-1) 

**Step 3 — `NetworkState::add_observed_addr` performs no validation.**
Lines 513–516: the address is inserted into `observed_addrs: RwLock<HashMap<SessionId, Multiaddr>>` with no further checks. [3](#0-2) 

**Step 4 — `local_listen_addrs` pads with attacker-controlled addresses.**
Lines 458–470: when the victim has fewer than `MAX_RETURN_LISTEN_ADDRS` (10) public addresses — the common case for NAT-ed nodes — `local_listen_addrs` fills the remainder from `observed_addrs`, directly including the attacker's injected address. [4](#0-3) 

**Step 5 — `connected` broadcasts the poisoned list to every new peer.**
Lines 206–237: when any new peer connects, the victim immediately sends an identify message whose `listen_addrs` is built from `local_listen_addrs()`. The only filter applied is `is_reachable`, which merely checks that the IP is globally routable — a condition the attacker trivially satisfies by using any real public IP (e.g., `8.8.8.8`). [5](#0-4) 

**Existing guards are insufficient.** The `is_reachable` filter at broadcast time (line 219) does not prevent injection of a real public IP. The `MAX_ADDRS` check in `process_listens` (line 134) applies only to the remote peer's *listen* addresses, not to the `observed_addr` field. No quorum or cross-peer confirmation is required before an observed address is promoted to the advertised list. [6](#0-5) 

## Impact Explanation
This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker maintaining simultaneous connections to many CKB nodes can inject chosen addresses into each victim's advertised address set at negligible cost (one TCP connection per victim). Each victim then propagates the poisoned address to every subsequent peer via the identify protocol. Those peers store it in their peer stores and may re-broadcast it via the discovery protocol, amplifying the pollution network-wide. At scale, peers repeatedly attempt connections to poisoned (non-existent or attacker-controlled) addresses, fail, and retry, generating abnormal connection churn and degrading effective network connectivity. NAT-ed nodes — the majority of CKB nodes — are fully exposed because they have zero configured public addresses and rely entirely on `observed_addrs` to fill their advertised list, meaning the attacker can control the entire advertised address set with a single connection. [7](#0-6) 

## Likelihood Explanation
The attack requires only a standard P2P connection — no special privileges, no PoW, no key material. The identify protocol is mandatory for all peers. The precondition (victim has fewer than 10 public addresses) is the default state for any node behind NAT. The `is_reachable` filter is the only guard and is trivially bypassed by using any real public IP. A single attacker can maintain many simultaneous connections to different victim nodes, keeping all poisoned entries alive for the duration of the connection. [8](#0-7) 

## Recommendation
1. **Validate ownership of `observed_addr` at insertion time:** In `process_observed`, reject any `observed_addr` whose IP does not match the actual transport address of the session (`session.address`). The observed address is supposed to reflect the local node's own external address as seen by the remote peer — it must correspond to the session's remote endpoint, not an arbitrary value supplied by the peer.
2. **Apply `is_reachable` at insertion time** in `add_observed_addr` / `NetworkState::add_observed_addr`, not only at broadcast time in `connected()`.
3. **Require quorum confirmation:** Only promote an observed address to the advertised list if multiple independent peers report the same address, preventing a single attacker from injecting an address.

## Proof of Concept
```
1. Attacker opens a TCP connection to victim node (standard P2P handshake).
2. Attacker sends an IdentifyMessage with:
     observed_addr = /ip4/8.8.8.8/tcp/8115
     (any globally routable IP the attacker controls or wishes to poison)
3. Victim's process_observed() calls add_observed_addr(observed, session_id)
   with no validation.
4. Victim stores /ip4/8.8.8.8/tcp/8115/p2p/<victim_peer_id> in observed_addrs.
5. A legitimate third peer connects to the victim.
6. Victim's connected() handler calls local_listen_addrs(), which pads with
   observed_addrs because victim has < 10 public addresses.
7. Victim sends IdentifyMessage to the third peer with listen_addrs containing
   /ip4/8.8.8.8/tcp/8115/p2p/<victim_peer_id>.
8. Third peer stores this in its peer store and may re-broadcast it via discovery.

Unit test assertion:
  - Construct an IdentifyProtocol with a mock IdentifyCallback backed by a
    NetworkState with zero configured public_addrs.
  - Simulate a connected session from attacker peer.
  - Deliver an IdentifyMessage with observed_addr = /ip4/8.8.8.8/tcp/8115.
  - Simulate a second peer connecting.
  - Assert that the outgoing IdentifyMessage to the second peer contains
    /ip4/8.8.8.8/tcp/8115/p2p/<local_peer_id> in its listen_addrs field.
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L24-24)
```rust
const MAX_RETURN_LISTEN_ADDRS: usize = 10;
```

**File:** network/src/protocols/identify/mod.rs (L134-149)
```rust
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

**File:** network/src/protocols/identify/mod.rs (L206-237)
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
