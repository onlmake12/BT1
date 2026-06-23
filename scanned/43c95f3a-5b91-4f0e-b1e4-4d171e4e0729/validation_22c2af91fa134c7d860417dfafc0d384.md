### Title
Unauthenticated `observed_addr` Injection Poisons Node's Advertised Listen Addresses — (`network/src/protocols/identify/mod.rs`)

### Summary

An unprivileged remote peer can inject an arbitrary globally-routable address into the victim node's `observed_addrs` store. Because `local_listen_addrs()` appends `observed_addrs` without any origin-validation, and the only downstream filter (`is_reachable`) only blocks private/loopback ranges, the injected address is included verbatim in every subsequent `IdentifyMessage` sent to newly-connecting peers. Those peers store the forged address in their peer stores and propagate it via the Discovery protocol, causing network-wide address-table corruption.

---

### Finding Description

**Step 1 — Injection (no validation in `add_observed_addr`)**

When the victim node receives an `IdentifyMessage` from peer A, `process_observed` calls `IdentifyCallback::add_observed_addr`: [1](#0-0) 

The function appends the local peer-id if missing, then stores the address unconditionally: [2](#0-1) 

There is no check that the address is actually the victim's own endpoint, that it matches the session's remote IP, or that it is reachable from the victim.

**Step 2 — Propagation via `local_listen_addrs`**

When peer B connects, `connected()` calls `local_listen_addrs()`: [3](#0-2) 

`listen_addrs()` returns only `public_addrs` (operator-configured). For a typical node with fewer than 10 configured public addresses, the remainder is filled from `observed_addrs` — including peer A's injected entry — with no origin check.

**Step 3 — The only guard (`is_reachable`) is insufficient**

`connected()` filters the result of `local_listen_addrs()` with `is_reachable`: [4](#0-3) 

`is_reachable` (from the tentacle/p2p library) only rejects private RFC-1918, loopback, and link-local ranges. Any globally-routable IP — including one the attacker controls or spoofs — passes this filter and is forwarded to peer B.

**Step 4 — Peer B stores and re-broadcasts the forged address**

`add_remote_listen_addrs` writes the received addresses directly into the peer store: [5](#0-4) 

The Discovery protocol subsequently gossips peer-store entries to the wider network.

**Lifetime note:** `observed_addrs` is keyed by `SessionId` and is cleaned up on session close: [6](#0-5) 

Peer A only needs to remain connected long enough for peer B to connect and receive the poisoned `IdentifyMessage`.

---

### Impact Explanation

Every peer that connects to the victim after the injection receives a forged globally-routable address as one of the victim's own listen addresses. Those peers:
- Store the address in their peer stores tagged with the victim's `PeerId` and capability flags.
- Propagate it via Discovery to the rest of the network.
- Attempt outbound connections to the forged address, wasting connection slots and degrading peer-graph connectivity.

The "consensus deviation" framing in the question is overstated. The direct, concrete impact is **network-wide address-table poisoning** for the victim node's identity, leading to degraded peer connectivity. Consensus itself is not directly broken, but sustained poisoning can partition the victim from honest peers.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no special privileges, no PoW, no key material.
- The injected address persists for the lifetime of peer A's session; a single long-lived connection is sufficient.
- The PoC address `192.0.2.1` (TEST-NET-1, RFC 5737) may be rejected by `is_reachable` depending on the tentacle implementation, but any real globally-routable IP (e.g., one the attacker controls) works identically.
- Exploitable on mainnet against any node that does not have 10 or more operator-configured `public_addresses`.

---

### Recommendation

1. **Validate observed address origin**: In `add_observed_addr`, verify that the IP component of the reported address matches the actual remote IP of the reporting session (`session.address`). Discard any observed address whose IP differs from the session's transport-layer source.
2. **Do not mix observed addrs into advertised listen addrs**: `observed_addrs` serve as a NAT-traversal hint for the node itself; they should not be forwarded to third parties as authoritative listen addresses. Remove the `observed_addrs` extension in `local_listen_addrs()`, or gate it behind a separate, clearly-labelled field in the `IdentifyMessage`.
3. **Bound the injection surface**: Limit `observed_addrs` to one entry per unique source IP (already partially done via `SessionId` keying) and add a rate-limit on updates.

---

### Proof of Concept

```
1. Start victim node V (no public_addresses configured).
2. Connect peer A to V.
3. Peer A sends IdentifyMessage with:
     observed_addr = /ip4/<attacker-controlled-global-IP>/tcp/9999
   (any globally-routable IP passes is_reachable)
4. V stores the address in observed_addrs[session_A].
5. Connect peer B to V.
6. V's connected() calls local_listen_addrs():
     public_addrs = [] (or < 10 entries)
     observed_addrs(10 - len) → includes /ip4/<attacker-IP>/tcp/9999/p2p/<V's peer-id>
7. V sends IdentifyMessage to B containing the forged address.
8. B calls add_remote_listen_addrs → peer_store.add_addr(/ip4/<attacker-IP>/tcp/9999/p2p/<V>)
9. B's Discovery protocol gossips this entry to the network.
10. Assert: capture B's peer store; confirm it contains <attacker-IP>:9999 attributed to V's PeerId.
```

No sanitization of the injected address occurs at `mod.rs:458–470` or `network.rs:513–516`.

### Citations

**File:** network/src/protocols/identify/mod.rs (L214-229)
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
