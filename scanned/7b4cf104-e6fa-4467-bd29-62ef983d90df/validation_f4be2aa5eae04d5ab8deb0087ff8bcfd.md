### Title
Discovery Protocol `misbehave` Enforces Disconnect But Not Ban, Allowing Persistent Reconnection by Misbehaving Peers — (File: `network/src/protocols/discovery/mod.rs`)

---

### Summary

The `DiscoveryAddressManager::misbehave` function, which is the enforcement point for all discovery-protocol misbehavior, only disconnects the offending peer and never bans its IP address. A `// FIXME:` comment in the code explicitly marks this as incomplete. Because no ban is recorded in the peer store, any unprivileged peer can reconnect immediately after being disconnected and repeat the misbehavior indefinitely. This is the CKB analog of the Ion Protocol finding: the authorization-enforcement layer acts on the wrong scope (session-level disconnect) instead of the correct scope (IP-level ban), leaving the intended protection unenforced.

---

### Finding Description

In `network/src/protocols/discovery/mod.rs`, the `DiscoveryAddressManager` implements the `AddressManager` trait. The `misbehave` method is the single enforcement callback for all discovery-protocol violations:

```rust
// network/src/protocols/discovery/mod.rs  lines 365-373
fn misbehave(&mut self, session: &SessionContext, behavior: &Misbehavior) -> MisbehaveResult {
    error!(
        "DiscoveryProtocol detects abnormal behavior, session: {:?}, behavior: {:?}",
        session, behavior
    );

    // FIXME:
    MisbehaveResult::Disconnect
}
``` [1](#0-0) 

This callback is invoked for every defined misbehavior variant:

| Trigger | Location |
|---|---|
| `DuplicateGetNodes` | line 110 |
| `DuplicateFirstNodes` | line 183 |
| `TooManyItems` | line 171–172 |
| `TooManyAddresses` | line 291–293 |
| `InvalidData` | line 214 | [2](#0-1) 

In every case the function returns `MisbehaveResult::Disconnect`, which causes the caller to call `context.disconnect(session.id)`. A disconnect closes the current TCP session but writes **nothing** to the peer store's ban list. The peer's IP address remains unbanned.

Compare this with how the sync and relay protocols handle misbehavior — they call `nc.ban_peer(peer_index, BAN_DURATION, reason)`, which invokes `NetworkState::ban_session`, which in turn calls `peer_store.ban_addr(...)` to record a timed IP-level ban:

```rust
// network/src/network.rs  lines 264-268
self.peer_store.lock().ban_addr(
    &peer.connected_addr,
    duration.as_millis() as u64,
    reason,
);
``` [3](#0-2) 

The `ban_addr` function converts the address to an `IpNetwork` and stores it in the `BanList`, which is checked on every new inbound connection attempt:

```rust
// network/src/peer_registry.rs  lines 109-111
if peer_store.is_addr_banned(&remote_addr) {
    return Err(PeerError::Banned.into());
}
``` [4](#0-3) 

Because `misbehave` never calls `ban_addr`, the ban-list gate is never set for discovery misbehaviors, and the peer can reconnect the moment the TCP session closes.

The `// FIXME:` comment at line 371 and the comment on the `AddressManager` trait itself (`// FIXME: Should be peer store?` in `addr.rs` line 40) confirm the developers recognized this enforcement gap but left it unresolved. [5](#0-4) 

---

### Impact Explanation

An unprivileged remote peer can:

1. Connect to any reachable CKB node.
2. Open the discovery sub-protocol.
3. Send a malformed or policy-violating discovery message (e.g., raw bytes that fail `decode`, a second `GetNodes` message, or a `Nodes` message with more than `MAX_ADDR_TO_SEND = 1000` items).
4. Receive a disconnect — but **no ban**.
5. Immediately reconnect and repeat from step 3.

Concrete consequences:
- **Resource exhaustion**: repeated TLS/Noise handshakes, session setup, and protocol negotiation consume CPU and memory on the victim node.
- **Discovery disruption**: the attacker occupies inbound connection slots, potentially preventing legitimate peers from connecting when `max_inbound` is reached.
- **Peer-store poisoning amplification**: before being disconnected, the attacker can inject up to `MAX_ADDR_TO_SEND` arbitrary addresses per cycle into the peer store via `add_new_addrs` (which ignores `_session_id` and applies no per-sender rate limit), since the ban that would stop future cycles is never issued. [6](#0-5) 

---

### Likelihood Explanation

**High.** The attack requires only a TCP connection to the node's P2P port (default `8115`) and the ability to speak the discovery sub-protocol (a simple length-prefixed binary format). No keys, no stake, no privileged role. The `// FIXME:` comment confirms the gap has existed since the code was written and has not been closed.

---

### Recommendation

Inside `DiscoveryAddressManager::misbehave`, call `NetworkState::ban_session` (or equivalent) to record an IP-level ban in the peer store before returning `MisbehaveResult::Disconnect`. The `session` parameter already carries the peer's address and session ID — the correct entity is available; it is simply not acted upon:

```rust
fn misbehave(&mut self, session: &SessionContext, behavior: &Misbehavior) -> MisbehaveResult {
    error!("...", session, behavior);
    // Ban the peer's IP so it cannot immediately reconnect.
    self.network_state.ban_session(
        &p2p_control,
        session.id,
        BAN_DURATION,
        format!("discovery misbehavior: {:?}", behavior),
    );
    MisbehaveResult::Disconnect
}
```

Additionally, apply a per-sender rate limit inside `add_new_addrs` using the currently-ignored `session_id` parameter to prevent address-store poisoning during the window before a ban takes effect.

---

### Proof of Concept

```
1. Connect to a CKB node on TCP port 8115.
2. Complete the Noise/secio handshake and open protocol ID for Discovery.
3. Send a raw byte sequence that fails decode() (e.g., 0x00 0x00 0x00 0x00).
   → Node calls misbehave(session, InvalidData) → returns Disconnect.
   → Node calls context.disconnect(session.id).
   → No entry is written to the ban list.
4. Immediately re-dial the same port.
5. Repeat steps 3–4 in a tight loop.
6. Confirm via RPC: get_banned_addresses() returns [] throughout.
   The node never bans the attacker's IP.
```

The existing integration test `MalformedMessage` (in `test/src/specs/p2p/malformed_message.rs`) demonstrates that the **Sync** protocol correctly bans `127.0.0.1/32` after two malformed messages. No equivalent test exists for the Discovery protocol, and no ban is issued there. [7](#0-6)

### Citations

**File:** network/src/protocols/discovery/mod.rs (L100-221)
```rust
        match decode(&data) {
            Some(item) => {
                match item {
                    DiscoveryMessage::GetNodes {
                        listen_port,
                        count,
                        version,
                        required_flags,
                    } => {
                        if let Some(state) = self.sessions.get_mut(&session.id) {
                            if state.received_get_nodes && check(Misbehavior::DuplicateGetNodes) {
                                if context.disconnect(session.id).await.is_err() {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                                return;
                            }

                            state.received_get_nodes = true;
                            // must get the item first, otherwise it is possible to load
                            // the address of peer listen.
                            let mut items = self.addr_mgr.get_random(2500, required_flags);

                            // change client random outbound port to client listen port
                            debug!("listen port: {:?}", listen_port);
                            if let Some(port) = listen_port {
                                state.remote_addr.update_port(port);
                                state.addr_known.insert(state.remote_addr.to_inner());
                                // add client listen address to manager
                                if let RemoteAddress::Listen(ref addr) = state.remote_addr {
                                    let flags = self.addr_mgr.node_flags(session.id);
                                    self.addr_mgr.add_new_addr(
                                        session.id,
                                        (addr.clone(), flags.unwrap_or(Flags::COMPATIBILITY)),
                                    );
                                }
                            }
                            if version >= state::REUSE_PORT_VERSION {
                                // after enable reuse port, it can be broadcast
                                state.remote_addr.change_to_listen();
                            }

                            let max = ::std::cmp::min(MAX_ADDR_TO_SEND, count as usize);
                            if items.len() > max {
                                items = items
                                    .choose_multiple(&mut rand::thread_rng(), max)
                                    .cloned()
                                    .collect();
                            }

                            state.addr_known.extend(items.iter());

                            let items = items
                                .into_iter()
                                .map(|addr| Node {
                                    addresses: vec![addr.0],
                                    flags: addr.1,
                                })
                                .collect::<Vec<_>>();

                            let nodes = Nodes {
                                announce: false,
                                items,
                            };

                            let msg = encode(DiscoveryMessage::Nodes(nodes));
                            if context.send_message(msg).await.is_err() {
                                debug!("{:?} send discovery msg Nodes fail", session.id)
                            }
                        }
                    }
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
                }
            }
            None => {
                if self
                    .addr_mgr
                    .misbehave(session, &Misbehavior::InvalidData)
                    .is_disconnect()
                    && context.disconnect(session.id).await.is_err()
                {
                    debug!("Disconnect {:?} msg failed to send", session.id)
                }
            }
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

**File:** network/src/protocols/discovery/mod.rs (L365-373)
```rust
    fn misbehave(&mut self, session: &SessionContext, behavior: &Misbehavior) -> MisbehaveResult {
        error!(
            "DiscoveryProtocol detects abnormal behavior, session: {:?}, behavior: {:?}",
            session, behavior
        );

        // FIXME:
        MisbehaveResult::Disconnect
    }
```

**File:** network/src/network.rs (L241-281)
```rust
    pub(crate) fn ban_session(
        &self,
        p2p_control: &ServiceControl,
        session_id: SessionId,
        duration: Duration,
        reason: String,
    ) {
        if let Some(addr) = self.with_peer_registry(|reg| {
            reg.get_peer(session_id)
                .filter(|peer| !peer.is_whitelist)
                .map(|peer| peer.connected_addr.clone())
        }) {
            info!(
                "Ban peer {:?} for {} seconds, reason: {}",
                addr,
                duration.as_secs(),
                reason
            );
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_network_ban_peer.inc();
            }
            if let Some(peer) = self.with_peer_registry_mut(|reg| reg.remove_peer(session_id)) {
                let message = format!("Ban for {} seconds, reason: {}", duration.as_secs(), reason);
                self.peer_store.lock().ban_addr(
                    &peer.connected_addr,
                    duration.as_millis() as u64,
                    reason,
                );
                if let Err(err) =
                    disconnect_with_message(p2p_control, peer.session_id, message.as_str())
                {
                    debug!("Disconnect failed {:?}, error: {:?}", peer.session_id, err);
                }
            }
        } else {
            debug!(
                "Ban session({}) failed: not found in peer registry or it is on the whitelist",
                session_id
            );
        }
    }
```

**File:** network/src/peer_registry.rs (L105-111)
```rust
        if !is_whitelist {
            if self.whitelist_only {
                return Err(PeerError::NonReserved.into());
            }
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** network/src/protocols/discovery/addr.rs (L40-51)
```rust
// FIXME: Should be peer store?
pub trait AddressManager {
    fn register(&self, id: SessionId, pid: ProtocolId, version: &str) -> bool;
    fn unregister(&self, id: SessionId, pid: ProtocolId);
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool;
    fn add_new_addr(&mut self, session_id: SessionId, addr: (Multiaddr, Flags));
    fn add_new_addrs(&mut self, session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>);
    fn misbehave(&mut self, session: &SessionContext, kind: &Misbehavior) -> MisbehaveResult;
    fn get_random(&mut self, n: usize, target: Flags) -> Vec<(Multiaddr, Flags)>;
    fn required_flags(&self) -> Flags;
    fn node_flags(&self, id: SessionId) -> Option<Flags>;
}
```

**File:** test/src/specs/p2p/malformed_message.rs (L1-46)
```rust
use crate::util::mining::out_ibd_mode;
use crate::utils::{sleep, wait_until};
use crate::{Net, Node, Spec};
use ckb_logger::info;
use ckb_network::{SupportProtocols, bytes::Bytes};
use ckb_types::{
    packed::{GetHeaders, SyncMessage},
    prelude::*,
};

pub struct MalformedMessage;

impl Spec for MalformedMessage {
    fn run(&self, nodes: &mut Vec<Node>) {
        info!("Run malformed message");
        info!("Connect node0");
        let node0 = &nodes[0];
        out_ibd_mode(nodes);
        let mut net = Net::new(self.name(), node0.consensus(), vec![SupportProtocols::Sync]);
        net.connect(node0);

        info!("Test node should receive GetHeaders message from node0");
        let ret = net.should_receive(node0, |data: &Bytes| {
            SyncMessage::from_slice(data)
                .map(|message| message.to_enum().item_name() == GetHeaders::NAME)
                .unwrap_or(false)
        });
        assert!(
            ret,
            "Test node should receive GetHeaders message from node0"
        );

        info!("Send malformed message to node0 twice");
        net.send(node0, SupportProtocols::Sync, vec![0, 0, 0, 0].into());
        net.send(node0, SupportProtocols::Sync, vec![0, 1, 2, 3].into());
        let rpc_client = nodes[0].rpc_client();
        let ret = wait_until(10, || rpc_client.get_peers().is_empty());
        assert!(ret, "Node0 should disconnect test node");
        let ret = wait_until(10, || {
            rpc_client
                .get_banned_addresses()
                .iter()
                .any(|ban| ban.address == "127.0.0.1/32")
        });
        assert!(ret, "Node0 should ban test node");
    }
```
