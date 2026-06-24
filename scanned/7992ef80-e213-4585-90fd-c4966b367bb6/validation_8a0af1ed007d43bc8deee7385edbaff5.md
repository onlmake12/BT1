The code matches the claim exactly. Both cited locations are confirmed:

- `network/src/network.rs` lines 688–719: `broadcast_exit_signals()` at line 717 is inside the outer `{ }` block but **outside** the `if let Some(id) = opt_session_id` guard at line 693. It executes unconditionally for both `Some` and `None`.
- `util/stop-handler/src/stop_register.rs` lines 65–79: `broadcast_exit_signals()` cancels `TOKIO_EXIT` and drains all crossbeam exit senders — a full node shutdown signal.

Audit Report

## Title
Unconditional `broadcast_exit_signals()` on Peer-Triggered Protocol Handler Panic Causes Node Shutdown - (File: `network/src/network.rs`)

## Summary
In `EventHandler::handle_error`, when tentacle surfaces `ServiceError::ProtocolHandleError` with `AbnormallyClosed(Some(session_id))` — indicating a session-scoped protocol handler panicked while processing a peer's message — the code bans the peer but then unconditionally calls `broadcast_exit_signals()`. This initiates a full graceful shutdown of the CKB node process, identical to receiving `SIGTERM`. A single remote peer that can cause any registered protocol handler to panic can therefore crash the local node.

## Finding Description
In `network/src/network.rs` at lines 688–719, the `ProtocolHandleError` match arm destructures `error` into `ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id)`. The `if let Some(id) = opt_session_id` guard at line 693 wraps only the `ban_session` call (lines 694–699). The `broadcast_exit_signals()` call at line 717 is outside that guard, inside the enclosing bare `{ }` block, and executes for both `Some(session_id)` (peer-triggered panic) and `None` (global handler panic) cases.

`broadcast_exit_signals()` (`util/stop-handler/src/stop_register.rs`, lines 65–79) sets `RECEIVED_STOP_SIGNAL`, cancels the global `TOKIO_EXIT` `CancellationToken`, and sends on all registered crossbeam exit channels — the same shutdown path as OS `SIGTERM`/`SIGINT`.

Tentacle wraps each session-scoped handler invocation (`received`, `connected`, `disconnected`) in `catch_unwind`. A panic in any of these methods is caught and re-surfaced as `ProtocolHandleError { error: AbnormallyClosed(Some(session_id)) }`. Any `unwrap()`/`expect()`/bounds-check failure inside a protocol handler's `received` method reachable via a crafted inbound message produces exactly this variant, triggering the unconditional shutdown.

Existing guards are insufficient: the `if let Some(id)` guard only protects the ban operation; there is no guard preventing `broadcast_exit_signals()` from running when `opt_session_id` is `Some`.

## Impact Explanation
A single unprivileged remote peer that can cause any session-scoped protocol handler to panic will crash the local CKB node process. This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points). The node performs a graceful shutdown and exits; it does not restart automatically unless managed by an external supervisor.

## Likelihood Explanation
The precondition is that at least one session-scoped protocol handler (sync, relay, discovery, identify, etc.) can be made to panic via a crafted inbound message. CKB's protocol handlers are non-trivial and use `unwrap()`/`expect()` in message-processing paths. The structural flaw is unconditional: any panic site — including those introduced by future code changes — is immediately weaponizable. An attacker needs only to open a connection and send a message that triggers a panic; no authentication or privilege is required. The attack is repeatable across node restarts.

## Recommendation
Make `broadcast_exit_signals()` conditional on `opt_session_id` being `None` (a global/service-level handler panic). For a per-session panic (`Some(id)`), ban the peer and continue operating:

```rust
ServiceError::ProtocolHandleError { proto_id, error } => {
    let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;
    if let Some(id) = opt_session_id {
        self.network_state.ban_session(
            &context.control().clone().into(),
            id,
            Duration::from_secs(300),
            format!("protocol {proto_id} panic when process peer message"),
        );
        error!("ProtocolHandleError: AbnormallyClosed (session), proto_id: {proto_id}, session id: {id:?}");
        // Do NOT call broadcast_exit_signals() — this is a per-peer fault
    } else {
        error!("ProtocolHandleError: AbnormallyClosed (global), proto_id: {proto_id}");
        broadcast_exit_signals();
    }
}
```

## Proof of Concept
1. Implement a `ServiceProtocol` whose `received` method unconditionally panics (e.g., `panic!("test")`).
2. Register it with the tentacle `ServiceBuilder` and start the service.
3. Open an inbound session and send any message on that protocol.
4. Tentacle's `catch_unwind` catches the panic and calls `handle_error` with `ProtocolHandleError { error: AbnormallyClosed(Some(session_id)) }`.
5. Observe: `ban_session` is called for `session_id`, then `broadcast_exit_signals()` is called unconditionally at line 717.
6. `TOKIO_EXIT` is cancelled and all crossbeam exit receivers fire; the node process exits.

The same outcome occurs with any existing protocol handler that can be made to panic via a crafted message, without requiring the synthetic protocol above. [1](#0-0) [2](#0-1)

### Citations

**File:** network/src/network.rs (L688-719)
```rust
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
```

**File:** util/stop-handler/src/stop_register.rs (L65-79)
```rust
pub fn broadcast_exit_signals() {
    debug!("Received exit signal; broadcasting exit signal to all threads");
    RECEIVED_STOP_SIGNAL.store(true, std::sync::atomic::Ordering::SeqCst);
    TOKIO_EXIT.cancel();
    CROSSBEAM_EXIT_SENDERS
        .lock()
        .iter()
        .for_each(|tx| match tx.try_send(()) {
            Ok(_) => {}
            Err(TrySendError::Full(_)) => info!("Ckb process has received exit signal"),
            Err(TrySendError::Disconnected(_)) => {
                debug!("broadcast thread: channel is disconnected")
            }
        });
}
```
