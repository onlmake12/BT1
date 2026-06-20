Based on my analysis of the actual production code, here is my determination:

---

### Title
Unbounded Peer Store Flooding via Repeated `Nodes(announce=true)` Discovery Messages — (`network/src/protocols/discovery/mod.rs`)

### Summary

An unprivileged remote peer can send an unlimited number of `Nodes(announce=true)` messages, each containing exactly `ANNOUNCE_THRESHOLD=10` items (3 addresses each), bypassing the only size check and injecting up to 30 addresses per message into the victim's peer store with no per-session rate limit.

### Finding Description

**Entrypoint:** Any peer with an active discovery protocol session.

**Root cause — off-by-one in `verify_nodes_message`:**

The guard uses strict greater-than: [1](#0-0) 

```rust
if nodes.items.len() > ANNOUNCE_THRESHOLD {  // ANNOUNCE_THRESHOLD = 10
    misbehavior = Some(Misbehavior::TooManyItems { ... });
}
```

A message with exactly 10 items passes this check unconditionally.

**Root cause — no per-session rate limit on incoming `announce=true` messages:**

In `received`, the only guard for duplicate `Nodes` messages applies exclusively to `announce=false`: [2](#0-1) 

```rust
if !nodes.announce && state.received_nodes {
    // DuplicateFirstNodes misbehavior — only fires for announce=false
} else {
    // announce=true falls here EVERY TIME, unconditionally
    state.addr_known.extend(addrs.iter());
    self.addr_mgr.add_new_addrs(session.id, addrs);
}
```

There is no counter, timestamp, or token-bucket tracking how many `announce=true` messages a session has sent. The `last_announce` field in `SessionState` is only used for rate-limiting *outgoing* announces via `check_timer`: [3](#0-2) 

It has no effect on incoming message processing.

**Root cause — `add_new_addrs` writes directly to peer store:** [4](#0-3) 

Each call to `add_new_addrs` iterates the addresses and calls `peer_store.add_addr` for each one that passes `is_valid_addr` (i.e., is a publicly reachable IP).

**Root cause — `check_purge` only triggers at capacity:** [5](#0-4) 

```rust
fn check_purge(&mut self) -> Result<()> {
    if self.addr_manager.count() < ADDR_COUNT_LIMIT {
        return Ok(());  // no-op until 16384 entries
    }
```

`ADDR_COUNT_LIMIT = 16384`: [6](#0-5) 

### Impact Explanation

An attacker sends 600 `Nodes(announce=true)` messages, each with 10 items × 3 addresses = 30 unique public addresses per message → 18,000 address injection attempts. The peer store fills to 16,384 entries with attacker-controlled addresses. Eviction (`check_purge`) only removes entries from over-represented network groups (>4 peers per /16 subnet), so an attacker spreading addresses across many subnets can survive eviction. Legitimate peer addresses are displaced, degrading the victim's ability to discover honest peers and setting up conditions for an eclipse attack.

### Likelihood Explanation

The attack requires only a single active P2P session — no special privileges, no PoW, no key material. The attacker simply sends crafted binary-encoded discovery messages in a tight loop. The `is_valid_addr` filter requires publicly routable IPs, but an attacker can trivially enumerate valid public IP space to craft valid addresses.

### Recommendation

1. Add a per-session counter for received `announce=true` Nodes messages and disconnect/misbehave after a threshold (e.g., more than a few per `ANNOUNCE_INTERVAL`).
2. Change the `verify_nodes_message` check from `>` to `>=` for announce messages, or reduce `ANNOUNCE_THRESHOLD` and use `>=`.
3. Apply a per-session rate limit (token bucket or timestamp gate) on how frequently `add_new_addrs` is called from a single session.

### Proof of Concept

```
1. Connect to victim node, complete handshake (discovery protocol registered).
2. In a loop, send Nodes { announce: true, items: [10 × Node { addresses: [addr1, addr2, addr3], flags: COMPATIBILITY }] }
   where addr1..addr3 are unique, publicly routable IPs.
3. After ~600 iterations: peer_store.addr_manager.count() → ~16384.
4. Observe that legitimate peer addresses are evicted or crowded out.
5. verify_nodes_message never fires (10 is not > 10); no disconnect occurs.
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L181-205)
```rust
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

**File:** network/src/protocols/discovery/mod.rs (L268-278)
```rust
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

**File:** network/src/protocols/discovery/state.rs (L94-109)
```rust
    pub(crate) fn check_timer(&mut self, now: Instant, interval: Duration) -> Option<&Multiaddr> {
        if self
            .last_announce
            .map(|time| now.saturating_duration_since(time) > interval)
            .unwrap_or(true)
        {
            self.last_announce = Some(now);
            if let RemoteAddress::Listen(addr) = &self.remote_addr {
                Some(addr)
            } else {
                None
            }
        } else {
            None
        }
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
