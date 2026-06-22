The key code is in `network/src/network.rs`. Let me confirm the exact lines and the `broadcast_exit_signals` behavior.

The evidence is already complete. Here is the analysis:

---

### Title
Unconditional `broadcast_exit_signals()` on Peer-Triggered Protocol Handler Panic Causes Node Shutdown - (`network/src/network.rs`)

### Summary

In `EventHandler::handle_error`, when tentacle emits `ServiceError::ProtocolHandleError { error: AbnormallyClosed(Some(session_id)) }` — which happens when a **session-scoped** protocol handler panics while processing a peer's message — the code bans the offending peer but then **unconditionally** calls `broadcast_exit_signals()`, initiating a full graceful shutdown of the CKB node process.

### Finding Description

In `network/src/network.rs`, the `ProtocolHandleError` arm of `handle_error` is:

```rust
ServiceError::ProtocolHandleError { proto_id, error } => {
    let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;
    {
        if let Some(id) = opt_session_id {
            self.network_state.ban_session(...);  // guards only the ban
        }
        // ...logging...
        broadcast_exit_signals();  // UNCONDITIONAL — runs for Some AND None
    }
}
``` [1](#0-0) 

The `if let Some(id)` guard only wraps `ban_session`. The `broadcast_exit_signals()` call on line 717 is **outside** that guard and executes regardless of whether `opt_session_id` is `Some(peer_session)` or `None`.

`broadcast_exit_signals()` cancels the global `TOKIO_EXIT` `CancellationToken` and sends on all registered crossbeam exit channels: [2](#0-1) 

This is the same signal path used by OS `SIGTERM`/`SIGINT` — it causes the entire node process to perform a graceful shutdown and exit.

`ProtocolHandleErrorKind::AbnormallyClosed(Some(session_id))` is emitted by tentacle when a **session-scoped** handler (e.g., `received`, `connected`, `disconnected`) panics. Tentacle wraps each handler invocation in `catch_unwind`; if the handler panics, tentacle catches it and surfaces it as this error variant with the triggering session's ID. Any `unwrap()`/`expect()`/bounds-check failure inside a protocol handler's `received` method — reachable via a malformed inbound message — produces exactly this variant.

### Impact Explanation

A single unprivileged remote peer that can cause any registered protocol handler to panic (via a malformed or unexpected message) will:
1. Have its session banned (5 minutes) — the only intended consequence.
2. **Also** trigger `broadcast_exit_signals()`, shutting down the entire local CKB node process.

This is a single-peer, single-message remote denial-of-service: the attacker's connection is banned, but the node exits.

### Likelihood Explanation

The precondition is that at least one protocol handler reachable from an inbound session can panic on a crafted message. CKB's protocol handlers (sync, relay, discovery, etc.) are non-trivial and use `unwrap()`/`expect()` in various places. Any such panic — even one introduced by a future code change — is immediately weaponizable because the shutdown path is unconditional. The structural flaw exists independently of whether a specific panic site is known today.

### Recommendation

The `broadcast_exit_signals()` call should be conditional on `opt_session_id` being `None` (i.e., a global/service-level handler panic, not a per-session one):

```rust
ServiceError::ProtocolHandleError { proto_id, error } => {
    let ProtocolHandleErrorKind::AbnormallyClosed(opt_session_id) = error;
    if let Some(id) = opt_session_id {
        self.network_state.ban_session(...);
        // log and continue — do NOT exit for a per-peer panic
    } else {
        // Global handler panic: no session to blame, safe to exit
        broadcast_exit_signals();
    }
}
``` [3](#0-2) 

### Proof of Concept

1. Register a `ServiceProtocol` whose `received` method unconditionally panics.
2. Connect an inbound session and send any message on that protocol.
3. Tentacle catches the panic and calls `handle_error` with `ProtocolHandleError { error: AbnormallyClosed(Some(session_id)) }`.
4. Observe that `broadcast_exit_signals()` is called: `TOKIO_EXIT` is cancelled and all crossbeam exit receivers fire.
5. The node process exits — triggered by a single peer message, with no `opt_session_id == None` guard preventing the shutdown path. [1](#0-0) [2](#0-1)

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
