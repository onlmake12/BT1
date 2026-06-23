### Title
Banned Peer Addresses Bypass Outbound Dial and Discovery Propagation Despite Ban List Enforcement — (`network/src/network.rs`, `network/src/peer_store/peer_store_impl.rs`)

### Summary

CKB's peer ban mechanism enforces the ban list at inbound connection acceptance (`accept_peer`) but omits the same check in the outbound dial gate (`can_dial`), the peer-selection functions used by `OutboundPeerService` (`fetch_addrs_to_feeler`, `fetch_addrs_to_attempt`), and the discovery address-sharing function (`fetch_random_addrs`). When a peer is banned via the `set_ban` RPC — which calls `ban_network()` rather than `ban_addr()` — the address is added to the ban list but is **not** removed from the addr_manager. As a result, the node continues to initiate outbound TCP connections to the banned peer and continues to advertise the banned peer's address to other nodes via the discovery protocol, undermining the operator's intent.

---

### Finding Description

CKB maintains a `BanList` inside `PeerStore` and exposes it through `is_addr_banned()`.

**Where the ban IS checked:**

`PeerRegistry::accept_peer()` checks `peer_store.is_addr_banned(&remote_addr)` and returns `Err(PeerError::Banned)` for non-whitelist peers: [1](#0-0) 

`PeerStore::add_addr()` and `add_outbound_addr()` check the ban list before inserting into the addr_manager: [2](#0-1) [3](#0-2) 

**Where the ban is NOT checked — the root cause:**

`NetworkState::can_dial()` is the single gate for every outbound dial. It checks for self-dial, own public address, already-connected peers, and in-progress dials — but **never consults the ban list**: [4](#0-3) 

`PeerStore::fetch_addrs_to_feeler()` selects addresses for feeler connections. Its filter checks connectivity recency and retry timing, but **no ban check**: [5](#0-4) 

`PeerStore::fetch_addrs_to_attempt()` selects addresses for regular outbound connections. Its filter also **omits a ban check**: [6](#0-5) 

`PeerStore::fetch_random_addrs()` returns addresses shared with other peers via the discovery protocol. Its filter **omits a ban check**: [7](#0-6) 

**The two-path ban asymmetry:**

`ban_addr()` (used for internally detected misbehaviour) removes the address from the addr_manager, so it disappears from all fetch functions: [8](#0-7) 

`ban_network()` (called by the `set_ban` RPC path) only inserts into the ban list and does **not** remove from the addr_manager: [9](#0-8) 

`NetworkController::ban()` — the function invoked by the `set_ban` RPC — calls `ban_network()`, not `ban_addr()`: [10](#0-9) 

`OutboundPeerService` periodically calls `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt`, then dials the returned addresses through `can_dial` / `dial_inner`, none of which check the ban list: [11](#0-10) [12](#0-11) 

---

### Impact Explanation

1. **Outbound connection attempts to banned peers**: After an operator issues `set_ban`, the banned IP's address remains in the addr_manager. `OutboundPeerService` will continue to select it via `fetch_addrs_to_attempt` and `fetch_addrs_to_feeler`, pass it through `can_dial` (which has no ban check), and issue a TCP dial. The connection is only rejected at `accept_peer` after the TCP handshake completes, meaning the banned peer receives repeated TCP connections from the local node. This wastes local resources and gives the banned peer a channel to observe the local node's dialing behaviour.

2. **Discovery propagation of banned addresses**: `fetch_random_addrs` is used by the discovery protocol to share known-good peer addresses with connected peers. Because it does not filter banned addresses, the local node continues to advertise the banned peer's address to the rest of the network, helping the banned peer maintain reachability despite the operator's explicit ban.

---

### Likelihood Explanation

The `set_ban` RPC is a standard operator tool documented in the CKB RPC reference and used in integration tests. Any operator who bans a peer via this RPC will trigger the condition. The banned peer's address persists in the addr_manager indefinitely (until the addr_manager evicts it by other criteria), so the window is long. No special attacker capability is required beyond being a peer that the operator decides to ban. [13](#0-12) 

---

### Recommendation

1. Add a ban-list check inside `can_dial()` in `network/src/network.rs`:
   ```rust
   if self.with_peer_store(|ps| ps.is_addr_banned(addr)) {
       return false;
   }
   ```

2. Add a ban-list check inside the closures of `fetch_addrs_to_feeler`, `fetch_addrs_to_attempt`, `fetch_random_addrs`, and `fetch_nat_addrs` in `network/src/peer_store/peer_store_impl.rs` so that banned addresses are excluded from all peer-selection and discovery-sharing paths.

3. Make `ban_network()` also remove matching addresses from the addr_manager (mirroring `ban_addr()`), so that the two ban paths are consistent.

---

### Proof of Concept

1. Start a CKB node A with a known peer B already in its addr_manager (previously connected).
2. Issue `set_ban` RPC on node A to ban peer B's IP.
3. Observe via debug logs that `OutboundPeerService` still selects B's address from `fetch_addrs_to_attempt` / `fetch_addrs_to_feeler` and issues a dial (TCP SYN visible on the wire).
4. Observe via a third node C connected to A that A's discovery responses still include B's address (returned by `fetch_random_addrs`), even though A has banned B.
5. Confirm that `ban_network()` was called (not `ban_addr()`), leaving B's entry in the addr_manager intact.

### Citations

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-74)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L103-106)
```rust
    pub fn add_outbound_addr(&mut self, addr: Multiaddr, flags: Flags) {
        if self.ban_list.is_addr_banned(&addr) {
            return;
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L201-212)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && peer_addr
                    .connected(|t| t > addr_expired_ms && t <= now_ms.saturating_sub(DIAL_INTERVAL))
                && required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
        };

        // get addrs that can attempt.
        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/peer_store_impl.rs (L230-239)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/peer_store_impl.rs (L276-282)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };

        // get success connected addrs.
        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/peer_store_impl.rs (L286-292)
```rust
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
        }
        self.addr_manager.remove(addr);
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L294-303)
```rust
    pub(crate) fn ban_network(&mut self, network: IpNetwork, timeout_ms: u64, ban_reason: String) {
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let ban_addr = BannedAddr {
            address: network,
            ban_until: now_ms + timeout_ms,
            created_at: now_ms,
            ban_reason,
        };
        self.mut_ban_list().ban(ban_addr);
    }
```

**File:** network/src/network.rs (L392-447)
```rust
    pub(crate) fn can_dial(&self, addr: &Multiaddr) -> bool {
        let peer_id = extract_peer_id(addr);
        if peer_id.is_none() {
            error!("Do not dial addr without peer id, addr: {}", addr);
            return false;
        }
        let peer_id = peer_id.as_ref().unwrap();

        if self.local_peer_id() == peer_id {
            trace!("Do not dial self: {:?}, {}", peer_id, addr);
            return false;
        }
        if self.public_addrs.read().contains(addr) {
            trace!(
                "Do not dial listened address(self): {:?}, {}",
                peer_id, addr
            );
            return false;
        }

        let peer_in_registry = self.with_peer_registry(|reg| {
            reg.get_key_by_peer_id(peer_id).is_some() || reg.is_feeler(addr)
        });
        if peer_in_registry {
            trace!("Do not dial peer in registry: {:?}, {}", peer_id, addr);
            return false;
        }

        if let Some(dial_started) = self.dialing_addrs.read().get(peer_id) {
            trace!(
                "Do not send repeated dial commands to network service: {:?}, {}",
                peer_id, addr
            );
            if Instant::now().saturating_duration_since(*dial_started) > DIAL_HANG_TIMEOUT {
                #[cfg(feature = "with_sentry")]
                with_scope(
                    |scope| scope.set_fingerprint(Some(&["ckb-network", "dialing-timeout"])),
                    || {
                        capture_message(
                            &format!(
                                "Dialing {:?}, {:?} for more than {} seconds, \
                                 something is wrong in network service",
                                peer_id,
                                addr,
                                DIAL_HANG_TIMEOUT.as_secs(),
                            ),
                            Level::Warning,
                        )
                    },
                );
            }
            return false;
        }

        true
    }
```

**File:** network/src/network.rs (L1422-1428)
```rust
    pub fn ban(&self, address: IpNetwork, ban_until: u64, ban_reason: String) {
        self.disconnect_peers_in_ip_range(address, &ban_reason);
        self.network_state
            .peer_store
            .lock()
            .ban_network(address, ban_until, ban_reason)
    }
```

**File:** network/src/services/outbound_peer.rs (L56-96)
```rust
    fn dial_feeler(&mut self) {
        let now_ms = unix_time_as_millis();
        let filter = |peer_addr: &AddrInfo| match self.transport_type {
            TransportType::Tcp => true,
            TransportType::Ws => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_) | Protocol::Tcp(_))),
            TransportType::Wss => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_))),
        };
        let attempt_peers = self.network_state.with_peer_store_mut(|peer_store| {
            let paddrs = peer_store.fetch_addrs_to_feeler(FEELER_CONNECTION_COUNT, filter);
            for paddr in paddrs.iter() {
                // mark addr as tried
                if let Some(paddr) = peer_store.mut_addr_manager().get_mut(&paddr.addr) {
                    paddr.mark_tried(now_ms);
                }
            }
            paddrs
        });

        trace!(
            "feeler dial count={}, attempt_peers: {:?}",
            attempt_peers.len(),
            attempt_peers,
        );

        for mut addr in attempt_peers.into_iter().map(|info| info.addr) {
            self.network_state.dial_feeler(&self.p2p_control, {
                match &self.transport_type {
                    TransportType::Tcp => (),
                    TransportType::Ws => addr.push(Protocol::Ws),
                    TransportType::Wss => addr.push(Protocol::Wss),
                }
                addr
            });
        }
    }
```

**File:** network/src/services/outbound_peer.rs (L98-184)
```rust
    fn try_dial_peers(&mut self) {
        let status = self.network_state.connection_status();
        let count = status
            .max_outbound
            .saturating_sub(status.non_whitelist_outbound) as usize;
        if count == 0 {
            self.try_identify_count = 0;
            return;
        }
        self.try_identify_count += 1;

        let target = &self.network_state.required_flags;

        let filter = |peer_addr: &AddrInfo| match self.transport_type {
            TransportType::Tcp => true,
            TransportType::Ws => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_) | Protocol::Tcp(_))),
            TransportType::Wss => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_))),
        };

        let f = |peer_store: &mut PeerStore, number: usize, now_ms: u64| -> Vec<AddrInfo> {
            let paddrs = peer_store.fetch_addrs_to_attempt(number, *target, filter);
            for paddr in paddrs.iter() {
                // mark addr as tried
                if let Some(paddr) = peer_store.mut_addr_manager().get_mut(&paddr.addr) {
                    paddr.mark_tried(now_ms);
                }
            }
            paddrs
        };

        let peers: Box<dyn Iterator<Item = Multiaddr>> = if self.try_identify_count > 3 {
            self.try_identify_count = 0;
            let len = self.network_state.bootnodes.len();
            if len < count {
                let now_ms = unix_time_as_millis();
                let attempt_peers = self
                    .network_state
                    .with_peer_store_mut(|peer_store| f(peer_store, count - len, now_ms));

                Box::new(
                    attempt_peers
                        .into_iter()
                        .map(|info| info.addr)
                        .chain(self.network_state.bootnodes.iter().cloned()),
                )
            } else {
                Box::new(
                    self.network_state
                        .bootnodes
                        .iter()
                        .choose_multiple(&mut rand::thread_rng(), count)
                        .into_iter()
                        .cloned(),
                )
            }
        } else {
            let now_ms = unix_time_as_millis();
            let attempt_peers = self
                .network_state
                .with_peer_store_mut(|peer_store| f(peer_store, count, now_ms));

            trace!(
                "identify dial count={}, attempt_peers: {:?}",
                attempt_peers.len(),
                attempt_peers,
            );

            Box::new(attempt_peers.into_iter().map(|info| info.addr))
        };

        for mut addr in peers {
            self.network_state.dial_identify(&self.p2p_control, {
                match &self.transport_type {
                    TransportType::Tcp => (),
                    TransportType::Ws => addr.push(Protocol::Ws),
                    TransportType::Wss => addr.push(Protocol::Wss),
                }
                addr
            });
        }
    }
```

**File:** rpc/src/module/net.rs (L691-727)
```rust
    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()> {
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
            }
            "delete" => {
                self.network_controller.unban(&ip_network);
                Ok(())
            }
            _ => Err(RPCError::invalid_params(format!(
                "Expected `params[1]` to be in the list [insert, delete], got {address}"
            ))),
        }
    }
```
