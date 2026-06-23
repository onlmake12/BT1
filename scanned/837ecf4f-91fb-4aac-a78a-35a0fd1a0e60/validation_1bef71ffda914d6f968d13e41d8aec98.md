I have all the code needed to make a definitive determination. Let me trace the exact logic.

**Key code facts confirmed:**

`can_dial` at line 420-444: when a `dialing_addrs` entry exists and `DIAL_HANG_TIMEOUT` (300s) is exceeded, the code fires a sentry warning but **unconditionally returns `false` without removing the stale entry**. [1](#0-0) 

`dial_success` and `dial_failed` are the only two removal paths for `dialing_addrs` entries: [2](#0-1) 

`dial_success` is called only on `SessionOpen`: [3](#0-2) 

`dial_failed` is called only on `DialerError`: [4](#0-3) 

The `SessionTimeout` handler does **not** call `dial_failed` and does not touch `dialing_addrs`: [5](#0-4) 

`dialing_addrs` is an unbounded `HashMap<PeerId, Instant>` with no capacity limit: [6](#0-5) 

---

### Title
Stale `dialing_addrs` entries permanently block outbound re-dialing after `DIAL_HANG_TIMEOUT` — (`network/src/network.rs`)

### Summary

`can_dial` detects that a dial has exceeded `DIAL_HANG_TIMEOUT` (300 s) but neither removes the stale `dialing_addrs` entry nor returns `true`. The entry persists forever, permanently preventing any future dial attempt to that `PeerId`. Because the only removal paths (`dial_success` / `dial_failed`) require a `SessionOpen` or `DialerError` event that a hanging transport never produces, the map grows without bound and the affected peers are permanently blacklisted from outbound dialing.

### Finding Description

`NetworkState::dialing_addrs` is a `RwLock<HashMap<PeerId, Instant>>` inserted into by `dial_inner` and removed from only by `dial_success` (on `SessionOpen`) and `dial_failed` (on `DialerError`). [7](#0-6) 

If the transport layer accepts a TCP connection but never completes the secio handshake (attacker-controlled peer), neither event fires. After 300 s, `can_dial` detects the stale entry at line 425 but the `return false` at line 443 executes unconditionally — the entry is never removed:

```rust
if let Some(dial_started) = self.dialing_addrs.read().get(peer_id) {
    // ... sentry warning only ...
    return false;   // ← stale entry stays; peer permanently blocked
}
``` [1](#0-0) 

The `SessionTimeout` error path, which could plausibly fire for a hung handshake, does not call `dial_failed` and leaves `dialing_addrs` untouched: [5](#0-4) 

### Impact Explanation

Each unique `PeerId` whose dial hangs permanently occupies one entry in `dialing_addrs`. The `OutboundPeerService` continuously fetches addresses from the peer store and calls `dial_identify`/`dial_feeler`. Once a `PeerId` is stuck, all future dial attempts to it are silently rejected by `can_dial`. An attacker who injects many unique addresses via the discovery protocol and runs servers that accept TCP but stall the handshake can exhaust the node's ability to establish new outbound connections, degrading sync and relay capability. Memory growth is proportional to the number of unique stalled `PeerId`s.

### Likelihood Explanation

The discovery protocol is reachable from any peer and can inject arbitrary addresses into the peer store. Running a server that accepts TCP connections but never sends secio bytes is trivial. The only mitigating uncertainty is whether the tentacle library enforces a handshake timeout shorter than 300 s that would produce a `DialerError` before `DIAL_HANG_TIMEOUT` is reached. If tentacle's handshake timeout ≥ 300 s or is absent for certain transport configurations, the path is fully exploitable. The `SessionTimeout` handler's failure to clean `dialing_addrs` is a confirmed secondary gap regardless.

### Recommendation

In `can_dial`, when `DIAL_HANG_TIMEOUT` is exceeded, remove the stale entry and return `true` so the dial can be retried:

```rust
if Instant::now().saturating_duration_since(*dial_started) > DIAL_HANG_TIMEOUT {
    // log / sentry ...
    drop(guard);
    self.dialing_addrs.write().remove(peer_id);
    return true;  // allow retry
}
return false;
```

Additionally, `handle_error` for `ServiceError::SessionTimeout` should call `self.network_state.dial_failed(&session_context.address)` to cover the handshake-timeout case.

### Proof of Concept

1. Start a CKB node.
2. Run N servers (unique `PeerId`s) that accept TCP but never write bytes.
3. Inject their addresses into the victim node via the discovery protocol.
4. Wait > 300 s.
5. Assert `dialing_addrs.len() == N` (entries never removed).
6. Assert `can_dial` returns `false` for all N peers despite the timeout having elapsed.
7. Observe the node can no longer initiate new outbound connections to those peers.

### Citations

**File:** network/src/network.rs (L79-79)
```rust
    dialing_addrs: RwLock<HashMap<PeerId, Instant>>,
```

**File:** network/src/network.rs (L420-444)
```rust
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
```

**File:** network/src/network.rs (L449-463)
```rust
    pub(crate) fn dial_success(&self, addr: &Multiaddr) {
        if let Some(peer_id) = extract_peer_id(addr) {
            self.dialing_addrs.write().remove(&peer_id);
        }
    }

    pub(crate) fn dial_failed(&self, addr: &Multiaddr) {
        self.with_peer_registry_mut(|reg| {
            reg.remove_feeler(addr);
        });

        if let Some(peer_id) = extract_peer_id(addr) {
            self.dialing_addrs.write().remove(&peer_id);
        }
    }
```

**File:** network/src/network.rs (L467-484)
```rust
    fn dial_inner(
        &self,
        p2p_control: &ServiceControl,
        addr: Multiaddr,
        target: TargetProtocol,
    ) -> Result<(), Error> {
        if !self.can_dial(&addr) {
            return Err(Error::Dial(format!("ignore dialing addr {addr}")));
        }

        debug!("Dialing {addr}");
        p2p_control.dial(addr.clone(), target)?;
        self.dialing_addrs.write().insert(
            extract_peer_id(&addr).expect("verified addr"),
            Instant::now(),
        );
        Ok(())
    }
```

**File:** network/src/network.rs (L611-641)
```rust
            ServiceError::DialerError { address, error } => {
                match error {
                    DialerErrorKind::HandshakeError(HandshakeErrorKind::SecioError(
                        SecioError::ConnectSelf,
                    )) => {
                        debug!("dial observed address success: {:?}", address);
                    }
                    DialerErrorKind::IoError(e)
                        if e.kind() == std::io::ErrorKind::AddrNotAvailable =>
                    {
                        warn!("DialerError({}) {}", address, e);
                    }
                    DialerErrorKind::TransportError(e)
                        if matches!(&e, TransportErrorKind::ProxyError(_proxy_err)) =>
                    {
                        let err = e.to_string();
                        if err.contains("failed to establish connection to target:General failure")
                            || err.contains(
                                "failed to establish connection to target:Connection refused",
                            )
                        {
                            debug!("DialerError({}) {}", address, e);
                        } else {
                            error!("Is the proxy server down? DialerError({}) {}", address, e);
                        }
                    }
                    _ => {
                        debug!("DialerError({}) {}", address, error);
                    }
                }
                self.network_state.dial_failed(&address);
```

**File:** network/src/network.rs (L658-663)
```rust
            ServiceError::SessionTimeout { session_context } => {
                debug!(
                    "SessionTimeout({}, {})",
                    session_context.id, session_context.address,
                );
            }
```

**File:** network/src/network.rs (L726-733)
```rust
            ServiceEvent::SessionOpen { session_context } => {
                debug!(
                    "SessionOpen({}, {})",
                    session_context.id, session_context.address,
                );

                self.network_state.dial_success(&session_context.address);

```
