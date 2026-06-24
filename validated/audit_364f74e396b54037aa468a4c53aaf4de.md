The code is confirmed exactly as described. Both files match the cited snippets precisely.

Key verification:
- [1](#0-0)  — `broadcast_exit_signals()` at line 717 is inside the same unconditional block as the conditional ban at lines 693–699. There is no `else` or early `return` after the ban; the shutdown fires regardless of whether `opt_session_id` is `Some` or `None`.
- [2](#0-1)  — `broadcast_exit_signals()` cancels `TOKIO_EXIT` and drains all crossbeam exit senders, which is a full process-wide shutdown signal.
- Other `ServiceError` arms (e.g., `ProtocolError`, `SessionTimeout`, `MuxerError`) do **not** call `broadcast_exit_signals()` — only `ProtocolHandleError` does, confirming this is not a general policy but a specific, unconditional call in this arm. [3](#0-2) 

The structural flaw is real and the code matches the report exactly.

---

Audit Report

## Title
Remote Peer Can Trigger Full Node Shutdown via Unconditional `broadcast_exit_signals()` in Protocol Handle Error Path — (`network/src/network.rs`)

## Summary
In `EventHandler::handle_error`, the `ServiceError::ProtocolHandleError` arm bans the offending peer session when a protocol handler panics with a peer-associated session ID, but then unconditionally calls `broadcast_exit_signals()` regardless. This causes a full, process-wide node shutdown even when the panic is directly attributable to a remote peer's crafted message. A connected peer who can trigger any panic in any CKB protocol handler's `received` callback can crash the local node.

## Finding Description
In `network/src/network.rs`, the `ProtocolHandleError` arm destructures the error as `ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id)`. When `opt_session_id` is `Some(id)`, the session is banned for 300 seconds: [4](#0-3) 

However, `broadcast_exit_signals()` is called unconditionally immediately after, within the same block, with no guard on whether the panic was peer-induced: [5](#0-4) 

`broadcast_exit_signals()` sets `RECEIVED_STOP_SIGNAL`, cancels the global `TOKIO_EXIT` `CancellationToken`, and sends to all registered crossbeam exit channels: [2](#0-1) 

This terminates every tokio task and crossbeam thread in the process. The tentacle P2P library wraps protocol handler callbacks in `catch_unwind`; any panic in a `received`, `connected`, or `disconnected` callback is surfaced as `ProtocolHandleErrorKind::AbnormallyClosed(Some(session_id))` — exactly the variant that reaches this code path. The presence of `Some(session_id)` is the signal that the panic was peer-induced, yet the code still shuts down the node. No other `ServiceError` arm calls `broadcast_exit_signals()`. [3](#0-2) 

## Impact Explanation
This is a **remote node crash** — matching the allowed CKB bounty impact: *"Vulnerabilities which could easily crash a CKB node"* — **High (10001–15000 points)**. A single connected peer can cause the target node to exit gracefully but unintentionally, taking it fully offline. If exploited at scale against multiple nodes simultaneously, it could degrade network availability.

## Likelihood Explanation
The design flaw is unconditional — it fires on any protocol handler panic, not just internal ones. CKB's sync and relay handlers process complex, attacker-controlled data structures. The precondition is a reachable panic (arithmetic overflow, `unwrap()` on `None`, index out of bounds, etc.) in any production handler's `received` callback. The attacker only needs to be a connected peer, which requires no privilege beyond establishing a P2P connection. The attack is repeatable: after the node restarts, the same peer (or another) can trigger it again.

## Recommendation
Guard `broadcast_exit_signals()` so it is only called when `opt_session_id` is `None` (indicating an internal/local panic with no peer context). When `opt_session_id` is `Some`, the session ban is sufficient — the node should log the error and continue operating:

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
        error!("Protocol handler panicked for peer session {id:?}, peer banned.");
        // Do NOT call broadcast_exit_signals() — this is peer-induced, not an internal bug.
    } else {
        error!("Protocol handler panicked with no session context — internal error, shutting down.");
        broadcast_exit_signals();
    }
}
```

## Proof of Concept
1. Implement a `CKBProtocol` handler whose `received` callback panics on a specific byte pattern (e.g., `panic!("test")` on any message).
2. Register it with the CKB network service in a test node.
3. Connect a peer and send the triggering message.
4. Observe via logs: the session ban fires (lines 694–699), then `broadcast_exit_signals()` is called (line 717), and the node exits.
5. Confirm the node process terminates despite the panic being fully attributable to the remote peer's message.
6. Alternatively: audit existing sync/relay handler `received` callbacks for reachable panics (e.g., unchecked `unwrap()`, slice indexing on attacker-controlled lengths) to identify a concrete triggering message without requiring a custom handler.

### Citations

**File:** network/src/network.rs (L643-687)
```rust
            ServiceError::ProtocolError {
                id,
                proto_id,
                error,
            } => {
                debug!("ProtocolError({}, {}) {}", id, proto_id, error);
                let message = format!("ProtocolError id={proto_id}");
                // Ban because misbehave of remote peer
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    id,
                    Duration::from_secs(300),
                    message,
                );
            }
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
```

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
