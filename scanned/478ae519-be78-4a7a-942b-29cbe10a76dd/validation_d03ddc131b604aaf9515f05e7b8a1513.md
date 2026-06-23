Based on my investigation of the code, here is my assessment:

---

### Title
Remote Peer Can Trigger Full Node Shutdown via Protocol Handler Panic — (`network/src/network.rs`)

### Summary

`EventHandler::handle_error` unconditionally calls `broadcast_exit_signals()` whenever any protocol handler panics, even when the panic is directly attributable to a remote peer's crafted message. The session ban that precedes the call is insufficient mitigation — the node still shuts down.

### Finding Description

In `handle_error`, the `ServiceError::ProtocolHandleError` arm correctly bans the offending session when `opt_session_id` is `Some`: [1](#0-0) 

However, immediately after the conditional ban, `broadcast_exit_signals()` is called **unconditionally**, regardless of whether the panic originated from a remote peer's message or an internal bug: [2](#0-1) 

`broadcast_exit_signals()` cancels the global `TOKIO_EXIT` `CancellationToken` and sends to all crossbeam exit channels: [3](#0-2) 

This terminates every tokio task and crossbeam thread in the process — a full, graceful-but-unintentional node shutdown.

The tentacle P2P library (which CKB uses) wraps protocol handler callbacks in `catch_unwind`. Any panic inside a `received`, `connected`, or `disconnected` callback is caught and surfaced as `ProtocolHandleErrorKind::AbnormallyClosed(Some(session_id))`, which is exactly the variant that reaches this code path. The `session_id` being `Some` is the signal that the panic was peer-induced, yet the code still calls `broadcast_exit_signals()`.

### Impact Explanation

A connected remote peer who can craft a message that triggers a panic (arithmetic overflow, `unwrap()` on `None`, index out of bounds, etc.) in any CKB protocol handler's `received` callback will cause the local node to exit. The node shuts down gracefully but unintentionally. This is a **remote crash / availability attack** requiring only a single connected peer.

### Likelihood Explanation

The design flaw is unconditional — it fires on **any** protocol handler panic, not just internal ones. CKB's sync and relay handlers process complex, attacker-controlled data structures. The precondition (a reachable panic in a production handler) is realistic given the complexity of the handlers. The attacker only needs to be a connected peer, which requires no privilege.

### Recommendation

Do not call `broadcast_exit_signals()` when `opt_session_id` is `Some`. A peer-induced panic should result only in session termination and peer ban, not global process exit. The call to `broadcast_exit_signals()` should be guarded:

```rust
ServiceError::ProtocolHandleError { proto_id, error } => {
    let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;
    if let Some(id) = opt_session_id {
        self.network_state.ban_session(...);
        // Log and return — do NOT shut down the node
        error!("Protocol handler panicked for peer session, banned peer.");
    } else {
        // No session context means internal/local panic — shut down is appropriate
        broadcast_exit_signals();
    }
}
```

### Proof of Concept

1. Implement a `CKBProtocol` handler whose `received` callback panics on a specific byte pattern.
2. Register it with the network service.
3. Connect a peer and send the triggering message.
4. Observe that `broadcast_exit_signals()` is called and the node exits, despite the panic being fully attributable to the remote peer's message.
5. Confirm via logs: the session ban fires first (line 694–699), then the node exits (line 717). [4](#0-3)

### Citations

**File:** network/src/network.rs (L688-718)
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
```

**File:** util/stop-handler/src/stop_register.rs (L65-78)
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
```
