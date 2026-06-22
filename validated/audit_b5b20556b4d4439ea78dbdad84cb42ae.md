The code is clear. Let me verify the exact lines for the critical path.

### Title
Stale `dialing_addrs` Entry Never Evicted After `DIAL_HANG_TIMEOUT` — Permanent Peer Undialability Until Restart - (`network/src/network.rs`)

### Summary

`NetworkState::can_dial` detects that a dial has been pending longer than `DIAL_HANG_TIMEOUT` (300 s) but only emits a sentry warning and still returns `false` without removing the stale `PeerId` entry from `dialing_addrs`. Because `dial_success` and `dial_failed` are the only two removal paths, and both require a `SessionOpen` or `DialerError` event that may never arrive for a hung connection, the entry persists indefinitely, permanently blocking future dial attempts to that peer until the node restarts.

### Finding Description

`dial_inner` inserts a `(PeerId, Instant::now())` record into `dialing_addrs` immediately after issuing the dial command: [1](#0-0) 

The two removal paths are:

- `dial_success` — called only from `handle_event` on `ServiceEvent::SessionOpen` [2](#0-1) 
- `dial_failed` — called only from `handle_error` on `ServiceError::DialerError` [3](#0-2) 

Every other `ServiceError` variant (`SessionTimeout`, `MuxerError`, `ProtocolSelectError`, `SessionBlocked`, `ProtocolHandleError`) is handled without calling either removal function. [4](#0-3) 

When `can_dial` detects the hang, it logs a sentry warning but unconditionally returns `false` **without removing the entry**: [5](#0-4) 

Once the 300 s threshold is crossed and the entry is still present, every subsequent call to `can_dial` for that peer hits the same branch, fires the same warning, and returns `false` again — the entry is never evicted.

### Impact Explanation

Every `PeerId` whose dial hangs past 300 s becomes permanently undialable on that node instance. An attacker who can get their address into the victim's peer store (trivially done via the discovery protocol) and then accept the TCP connection without completing the secio handshake will cause the victim to accumulate stale entries. With enough peer IDs the victim's outbound slot budget is effectively exhausted, degrading sync and relay connectivity.

### Likelihood Explanation

The scenario is reachable without any privileged access:

1. Attacker advertises one or more addresses via the discovery protocol.
2. Victim dials; attacker's host accepts the TCP SYN but never sends secio data.
3. If tentacle fires `SessionTimeout` (not `DialerError`) for the hung handshake, `dial_failed` is never called.
4. After 300 s the entry is detected but not removed; the peer is permanently blocked.

Silent TCP hangs (e.g., stateful firewall mid-path, or deliberate attacker behaviour) are common in real network deployments.

### Recommendation

In `can_dial`, when the elapsed time exceeds `DIAL_HANG_TIMEOUT`, drop the write lock and remove the stale entry before returning `true` to allow a fresh dial attempt:

```rust
// inside can_dial, replace the hang-detection block:
if Instant::now().saturating_duration_since(*dial_started) > DIAL_HANG_TIMEOUT {
    // log / sentry as before …
    drop(guard);                                   // release read lock
    self.dialing_addrs.write().remove(peer_id);    // evict stale entry
    return true;                                   // allow retry
}
return false;
```

Additionally, `SessionTimeout` and other non-`DialerError` service errors that correspond to outbound sessions should also call `dial_failed` to close the gap in cleanup coverage.

### Proof of Concept

```rust
// Insert a stale entry simulating a hung dial
let peer_id = PeerId::random();
network_state.dialing_addrs.write().insert(
    peer_id.clone(),
    Instant::now() - Duration::from_secs(301),   // past DIAL_HANG_TIMEOUT
);

// Build a multiaddr containing that peer_id
let addr: Multiaddr = format!("/ip4/1.2.3.4/tcp/8115/p2p/{}", peer_id.to_base58())
    .parse().unwrap();

// can_dial returns false — peer is permanently blocked
assert!(!network_state.can_dial(&addr));

// Entry is still present — never evicted
assert!(network_state.dialing_addrs.read().contains_key(&peer_id));

// A second call 60 s later still returns false
assert!(!network_state.can_dial(&addr));
```

### Citations

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

**File:** network/src/network.rs (L479-482)
```rust
        self.dialing_addrs.write().insert(
            extract_peer_id(&addr).expect("verified addr"),
            Instant::now(),
        );
```

**File:** network/src/network.rs (L641-641)
```rust
                self.network_state.dial_failed(&address);
```

**File:** network/src/network.rs (L658-720)
```rust
            ServiceError::SessionTimeout { session_context } => {
                debug!(
                    "SessionTimeout({}, {})",
                    session_context.id, session_context.address,
                );
            }
            ServiceError::MuxerError {
                session_context,
                error,
            } => {
                debug!(
                    "MuxerError({}, {}), substream error {}, disconnect it",
                    session_context.id, session_context.address, error,
                );
            }
            ServiceError::ListenError { address, error } => {
                debug!("ListenError: address={:?}, error={:?}", address, error);
            }
            ServiceError::ProtocolSelectError {
                proto_name,
                session_context,
            } => {
                debug!(
                    "ProtocolSelectError: proto_name={:?}, session_id={}",
                    proto_name, session_context.id,
                );
            }
            ServiceError::SessionBlocked { session_context } => {
                debug!("SessionBlocked: {}", session_context.id);
            }
            ServiceError::ProtocolHandleError { proto_id, error } => {
                debug!("ProtocolHandleError: {:?}, proto_id: {}", error, proto_id);

                let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;
                {
                    if let Some(id) = opt_session_id {
                        self.network_state.ban_session(
                            &context.control().clone().into(),
                            id,
                            Duration::from_secs(300),
                            format!("protocol {proto_id} panic when process peer message"),
                        );
                    }
                    #[cfg(feature = "with_sentry")]
                    with_scope(
                        |scope| scope.set_fingerprint(Some(&["ckb-network", "p2p-service-error"])),
                        || {
                            capture_message(
                                &format!(
                                    "ProtocolHandleError: AbnormallyClosed, proto_id: {opt_session_id:?}, session id: {opt_session_id:?}"
                                ),
                                Level::Warning,
                            )
                        },
                    );
                    error!(
                        "ProtocolHandleError: AbnormallyClosed, proto_id: {opt_session_id:?}, session id: {opt_session_id:?}"
                    );

                    broadcast_exit_signals();
                }
            }
        }
```

**File:** network/src/network.rs (L732-732)
```rust
                self.network_state.dial_success(&session_context.address);
```
