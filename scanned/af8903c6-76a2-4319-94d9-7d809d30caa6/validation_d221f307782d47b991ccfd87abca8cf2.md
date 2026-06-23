Looking at the code carefully, I need to trace the exact path for `announce=true` Nodes messages and check for any rate-limiting guards.

The code is fully readable. Let me now do a precise analysis of the control flow.

The attack path is concrete and traceable through the production code:

**Step 1 — Entry point**: An unprivileged peer sends a `DiscoveryMessage::Nodes` with `announce=true`, 10 items, 3 addresses each.

**Step 2 — `verify_nodes_message` check** (`mod.rs` lines 266–300):
- `announce=true` branch checks `nodes.items.len() > ANNOUNCE_THRESHOLD` (i.e., `> 10`). Exactly 10 items → passes.
- Per-item check: `item.addresses.len() > MAX_ADDRS` (i.e., `> 3`). Exactly 3 → passes.
- Returns `None` → no misbehavior, no disconnect.

**Step 3 — `received()` branch logic** (`mod.rs` lines 180–206):
- The one-time guard `state.received_nodes` is only set and checked for `!nodes.announce` (line 181–203). For `announce=true`, this guard is **never consulted**.
- Execution falls through to the `else` block unconditionally, collects all 30 addresses, and calls `self.addr_mgr.add_new_addrs(session.id, addrs)` (line 205).

**Step 4 — `add_new_addrs` lock acquisition** (`mod.rs` lines 347–363):
- For each of the 30 addresses, calls `self.network_state.with_peer_store_mut(...)` (line 354), which acquires `self.peer_store.lock()` (`network.rs` line 322) — a blocking Mutex — once per address.

**Step 5 — No rate limit, no counter, no disconnect**: The attacker can repeat this indefinitely. The `addr_known` bloom filter (line 197) records the addresses but does **not** gate the `add_new_addrs` call — it is only used for outgoing message deduplication.

The asymmetry is clear: `announce=false` has a hard one-time guard (`received_nodes` flag), while `announce=true` has no per-session counter or rate limit at all.

---

### Title
Unbounded `announce=true` Nodes message processing allows sustained peer-store lock contention — (`network/src/protocols/discovery/mod.rs`)

### Summary
The `DiscoveryProtocol::received` handler applies a one-time delivery guard only to `announce=false` Nodes messages. `announce=true` messages have no per-session counter or rate limit. A remote peer can send an unlimited stream of well-formed `announce=true` Nodes messages (10 items × 3 addresses = 30 addresses each), each of which unconditionally invokes `add_new_addrs`, which acquires the global `peer_store` Mutex 30 times per message, causing sustained lock contention that degrades all other operations sharing that lock.

### Finding Description

`verify_nodes_message` enforces only a per-message size ceiling (`> ANNOUNCE_THRESHOLD` for announce, `> MAX_ADDR_TO_SEND` for non-announce), not a per-session message-count limit. [1](#0-0) 

In `received()`, the `received_nodes` one-time guard is checked and set only when `!nodes.announce`: [2](#0-1) 

For `announce=true`, execution always reaches `add_new_addrs` with no session-level counter: [3](#0-2) 

`add_new_addrs` acquires the global peer-store Mutex once per address: [4](#0-3) 

`with_peer_store_mut` holds the lock for the entire callback duration: [5](#0-4) 

### Impact Explanation

The `peer_store` Mutex is shared across peer connection acceptance, feeler dialing, outbound peer management, address dumping, and the identify protocol. Sustained lock acquisition at 30 locks/message × N messages/second starves all these operations. The discovery protocol handler runs on the p2p async runtime; blocking lock acquisition inside it delays processing of all other protocol messages on the same executor thread. [6](#0-5) 

### Likelihood Explanation

Any peer that can establish a TCP connection to the node can exploit this. No authentication, PoW, or privileged role is required. The attacker only needs to craft valid `announce=true` Nodes messages at exactly the allowed threshold (10 items, 3 addresses each), which is trivially constructable. The node has no mechanism to detect or disconnect such a peer. [7](#0-6) 

### Recommendation

Add a per-session counter for `announce=true` messages (analogous to `received_nodes` for `announce=false`), or apply a time-based rate limit (e.g., one announce batch per `ANNOUNCE_INTERVAL`). The `SessionState` struct already tracks `received_get_nodes` and `received_nodes` as boolean guards; a similar `last_announce_received: Option<Instant>` field with a minimum interval check would close this gap. [8](#0-7) 

### Proof of Concept

State-test with a mock `AddressManager` that counts `add_new_addrs` invocations:

1. Instantiate `DiscoveryProtocol` with a mock `AddressManager` that increments a counter on each `add_new_addrs` call and records the total address count.
2. Simulate `connected()` for one session.
3. Send N `DiscoveryMessage::Nodes(Nodes { announce: true, items: [Node { addresses: [a1,a2,a3], flags: _ }; 10] })` messages through `received()`.
4. Assert: `add_new_addrs` was called N times, total addresses processed = N × 30, no disconnect was triggered.

The assertion will hold for any N, demonstrating the absence of any per-session rate limit on `announce=true` message processing. [9](#0-8)

### Citations

**File:** network/src/protocols/discovery/mod.rs (L29-34)
```rust
const ANNOUNCE_CHECK_INTERVAL: Duration = Duration::from_secs(60);
const ANNOUNCE_THRESHOLD: usize = 10;
// The maximum number of new addresses to accumulate before announcing.
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L170-208)
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
                            }
                        }
                    }
```

**File:** network/src/protocols/discovery/mod.rs (L266-299)
```rust
fn verify_nodes_message(nodes: &Nodes) -> Option<Misbehavior> {
    let mut misbehavior = None;
    if nodes.announce {
        if nodes.items.len() > ANNOUNCE_THRESHOLD {
            warn!(
                "Number of nodes exceeds announce threshold {}",
                ANNOUNCE_THRESHOLD
            );
            misbehavior = Some(Misbehavior::TooManyItems {
                announce: nodes.announce,
                length: nodes.items.len(),
            });
        }
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }

    if misbehavior.is_none() {
        for item in &nodes.items {
            if item.addresses.len() > MAX_ADDRS {
                misbehavior = Some(Misbehavior::TooManyAddresses(item.addresses.len()));
                break;
            }
        }
    }

    misbehavior
```

**File:** network/src/protocols/discovery/mod.rs (L352-362)
```rust
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
```

**File:** network/src/network.rs (L283-299)
```rust
    pub(crate) fn accept_peer(
        &self,
        session_context: &SessionContext,
    ) -> Result<Option<Peer>, Error> {
        // NOTE: be careful, here easy cause a deadlock,
        //    because peer_store's lock scope across peer_registry's lock scope
        let mut peer_store = self.peer_store.lock();

        {
            self.peer_registry.write().accept_peer(
                session_context.address.clone(),
                session_context.id,
                session_context.ty,
                &mut peer_store,
            )
        }
    }
```

**File:** network/src/network.rs (L318-323)
```rust
    pub(crate) fn with_peer_store_mut<F, T>(&self, callback: F) -> T
    where
        F: FnOnce(&mut PeerStore) -> T,
    {
        callback(&mut self.peer_store.lock())
    }
```

**File:** network/src/protocols/discovery/state.rs (L28-37)
```rust
pub struct SessionState {
    // received pending messages
    pub(crate) addr_known: AddrKnown,
    // FIXME: Remote listen address, resolved by id protocol
    pub(crate) remote_addr: RemoteAddress,
    last_announce: Option<Instant>,
    pub(crate) announce_multiaddrs: Vec<(Multiaddr, Flags)>,
    pub(crate) received_get_nodes: bool,
    pub(crate) received_nodes: bool,
}
```
