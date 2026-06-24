Audit Report

## Title
Remote Peer Can Trigger Full Node Shutdown via Protocol Handler Panic — (File: network/src/network.rs)

## Summary
In `EventHandler::handle_error`, the `ServiceError::ProtocolHandleError` arm bans the offending peer session when `opt_session_id` is `Some`, but then unconditionally calls `broadcast_exit_signals()` regardless of whether the panic was peer-induced or internal. `broadcast_exit_signals()` cancels the global `TOKIO_EXIT` cancellation token and signals all crossbeam threads, causing a full node shutdown. A connected remote peer who can craft a message that triggers any panic in a protocol handler's `received` callback can therefore crash the local node.

## Finding Description
In `network/src/network.rs` at line 688, the `ServiceError::ProtocolHandleError { proto_id, error }` arm destructures `error` as `ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id)`. [1](#0-0) 

When `opt_session_id` is `Some(id)`, the session is banned for 300 seconds (lines 693–699). [2](#0-1) 

However, `broadcast_exit_signals()` is called unconditionally at line 717, inside the same block, with no guard on whether `opt_session_id` is `Some` or `None`. [3](#0-2) 

`broadcast_exit_signals()` sets `RECEIVED_STOP_SIGNAL`, cancels `TOKIO_EXIT`, and drains all crossbeam exit sender channels — terminating every tokio task and crossbeam thread in the process. [4](#0-3) 

The tentacle P2P library wraps protocol handler callbacks (`received`, `connected`, `disconnected`) in `catch_unwind`. Any panic inside those callbacks is surfaced as `ProtocolHandleErrorKind::AbnormallyClosed(Some(session_id))`, which is exactly the variant that reaches this code path. The `Some(session_id)` is the signal that the panic was peer-induced, yet the code still calls `broadcast_exit_signals()`. The session ban is insufficient mitigation because the node shuts down immediately after.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Vulnerabilities which could easily crash a CKB node."* A single connected remote peer who can craft a message triggering any panic (arithmetic overflow, `unwrap()` on `None`, index out-of-bounds, etc.) in any CKB protocol handler's `received` callback causes the local node to exit. The shutdown is graceful but unintentional and fully attacker-controlled.

## Likelihood Explanation
The design flaw is unconditional — it fires on any protocol handler panic, not just internal ones. CKB's sync and relay handlers process complex, attacker-controlled data structures. The attacker only needs to be a connected peer (no special privilege), and the precondition — a reachable panic in any production handler — is realistic given the complexity of the handlers. The attack is repeatable: after the node restarts, the attacker can reconnect and trigger it again.

## Recommendation
Guard the `broadcast_exit_signals()` call on `opt_session_id` being `None`. When the session ID is present, the panic is attributable to a remote peer; only the session ban is appropriate. When the session ID is absent, the panic is internal and shutdown is appropriate:

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
        error!("Protocol handler panicked for peer session {id:?}, banned peer. Not shutting down.");
        // Do NOT call broadcast_exit_signals() here
    } else {
        error!("Protocol handler panicked with no session context (internal bug). Shutting down.");
        broadcast_exit_signals();
    }
}
```

## Proof of Concept
1. Implement a `CKBProtocol` handler whose `received` callback panics on a specific byte pattern (e.g., `panic!("test")` when the first byte is `0xFF`).
2. Register it with the network service and start a CKB node.
3. Connect a peer and send a message matching the triggering pattern.
4. The tentacle library catches the panic via `catch_unwind` and emits `ProtocolHandleErrorKind::AbnormallyClosed(Some(session_id))`.
5. Observe in logs: the session ban fires (lines 694–699), then `broadcast_exit_signals()` is called (line 717), and the node exits.
6. Confirm the node is fully terminated despite the panic being entirely attributable to the remote peer's message.

### Citations

**File:** network/src/network.rs (L688-691)
```rust
            ServiceError::ProtocolHandleError { proto_id, error } => {
                debug!("ProtocolHandleError: {:?}, proto_id: {}", error, proto_id);

                let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;
```

**File:** network/src/network.rs (L693-699)
```rust
                    if let Some(id) = opt_session_id {
                        self.network_state.ban_session(
                            &context.control().clone().into(),
                            id,
                            Duration::from_secs(300),
                            format!("protocol {proto_id} panic when process peer message"),
                        );
```

**File:** network/src/network.rs (L717-717)
```rust
                    broadcast_exit_signals();
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
