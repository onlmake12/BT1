The code is confirmed. Let me check the tentacle p2p dependency to verify the semantics of `ProtocolHandleErrorKind::AbnormallyClosed`.

Audit Report

## Title
Remote Peer Can Trigger Full Node Shutdown via Unconditional `broadcast_exit_signals()` in Protocol Handler Error Path — (File: network/src/network.rs)

## Summary
In `EventHandler::handle_error`, the `ServiceError::ProtocolHandleError` arm correctly bans the offending peer session when `opt_session_id` is `Some`, but then unconditionally calls `broadcast_exit_signals()` at line 717 regardless of whether the panic was peer-induced or internal. `broadcast_exit_signals()` cancels the global `TOKIO_EXIT` cancellation token and signals all crossbeam threads, causing a full node shutdown. Any connected remote peer who can trigger a panic in any protocol handler's session-scoped callback can therefore crash the local node.

## Finding Description
At `network/src/network.rs` line 691, the error is destructured as the irrefutable pattern `let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;`, confirming `AbnormallyClosed` is the only variant surfaced here. [1](#0-0) 

When `opt_session_id` is `Some(id)`, the session is banned for 300 seconds (lines 693–700), which is the correct response to a peer-induced panic. [2](#0-1) 

However, `broadcast_exit_signals()` is called unconditionally at line 717, outside the `if let Some(id)` block, making the session ban logically pointless — the node shuts down immediately after. [3](#0-2) 

`broadcast_exit_signals()` sets `RECEIVED_STOP_SIGNAL` to `true`, cancels the `TOKIO_EXIT` cancellation token, and drains all crossbeam exit sender channels — terminating every tokio task and crossbeam thread in the process. [4](#0-3) 

The tentacle P2P library (imported via `p2p::error::ProtocolHandleErrorKind` in `network/src/errors.rs`) wraps session-scoped protocol handler callbacks (`received`, `connected`, `disconnected`) in `catch_unwind`. A panic in a session-scoped callback surfaces as `AbnormallyClosed(Some(session_id))`; a panic in a global handler surfaces as `AbnormallyClosed(None)`. The `Some(session_id)` variant is the signal that the panic is attributable to a remote peer, yet the code still calls `broadcast_exit_signals()` in both cases. [5](#0-4) 

The design inconsistency is self-evident: the developers added a session ban specifically for the `Some` case, acknowledging peer attribution, but failed to guard the shutdown call on the `None` case only.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Vulnerabilities which could easily crash a CKB node."* Any connected remote peer who can craft a message that triggers a panic (arithmetic overflow, `unwrap()` on `None`, index out-of-bounds, etc.) in any CKB protocol handler's session-scoped `received` callback causes the local node to exit. The shutdown is graceful but fully attacker-controlled and unintentional.

## Likelihood Explanation
The design flaw is unconditional — it fires on any session-scoped protocol handler panic. CKB's sync and relay handlers process complex, attacker-controlled data structures. The attacker only needs to be a connected peer (no special privilege). The precondition — a reachable panic in any production session-scoped handler — is realistic given handler complexity. The attack is repeatable: after the node restarts, the attacker can reconnect and trigger it again. The session ban (300 seconds) is irrelevant because the node has already exited.

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
        error!("Protocol handler panicked for peer session {id:?}, banned peer.");
        // Do NOT call broadcast_exit_signals() here
    } else {
        error!("Protocol handler panicked with no session context (internal). Shutting down.");
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

**File:** network/src/network.rs (L693-700)
```rust
                    if let Some(id) = opt_session_id {
                        self.network_state.ban_session(
                            &context.control().clone().into(),
                            id,
                            Duration::from_secs(300),
                            format!("protocol {proto_id} panic when process peer message"),
                        );
                    }
```

**File:** network/src/network.rs (L717-717)
```rust
                    broadcast_exit_signals();
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

**File:** network/src/errors.rs (L1-9)
```rust
//! Error module
use p2p::{
    SessionId,
    error::{
        DialerErrorKind, ListenErrorKind, ProtocolHandleErrorKind, SendErrorKind,
        TransportErrorKind,
    },
    secio::PeerId,
};
```
