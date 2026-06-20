The code is confirmed. Let me verify the channel size and the `misbehave` return value one more time before writing the final assessment.

The critical facts from the code:

**`notify` (lines 316–325):** iterates `&self.remote_infos`, calls `context.disconnect(*session_id).await` for timed-out sessions, but **never removes the entry from `remote_infos`**. [1](#0-0) 

**`disconnected` (lines 239–248):** the **only** place that removes entries from `remote_infos`. [2](#0-1) 

**`misbehave` (lines 509–514):** unconditionally returns `MisbehaveResult::Disconnect` for every reason including `Misbehavior::Timeout`. [3](#0-2) 

**Tentacle inner channel size:** `DEFAULT_CHANNEL_SIZE = 1024`. [4](#0-3) 

---

### Title
Repeated `disconnect()` Storm in `IdentifyProtocol::notify` Due to Missing `remote_infos` Cleanup — (`network/src/protocols/identify/mod.rs`)

### Summary
`IdentifyProtocol::notify` calls `context.disconnect(session_id)` for every timed-out session on every 1-second tick but never removes the entry from `remote_infos`. Removal only happens in `disconnected()`, which is called asynchronously after the transport actually closes. An attacker who opens `max_inbound` connections and never sends an Identify message causes the node to emit O(`max_inbound`) disconnect commands per second, indefinitely, until each transport closes — potentially saturating the tentacle service control channel (capacity 1024) and starving legitimate protocol operations.

### Finding Description
In `IdentifyProtocol::notify` (lines 316–325), the loop iterates over `&self.remote_infos` and, for each session where `!has_received && elapsed >= timeout`, calls `self.callback.misbehave(…)` (which always returns `Disconnect`) and then `context.disconnect(*session_id).await`. The entry is **not** removed from `remote_infos` at this point. [1](#0-0) 

The entry is only removed in `disconnected()`: [5](#0-4) 

`disconnected()` is invoked by the tentacle runtime only after the underlying transport fully closes — which can be delayed if the remote peer holds the TCP connection open (e.g., by not acknowledging FIN). During that window, every subsequent `notify` tick re-evaluates the same session, finds `!has_received && elapsed >= timeout` still true, and issues another `disconnect` command.

`misbehave` always returns `Disconnect` unconditionally: [3](#0-2) 

### Impact Explanation
The tentacle service control channel has a bounded capacity of 1024: [4](#0-3) 

With `max_inbound` timed-out sessions (e.g., 125), the node emits ~125 `disconnect` commands per second. If the remote peers hold TCP connections open (a trivial client-side action), the window extends to many seconds. At 125 commands/second, the 1024-slot channel fills in under 9 seconds. Once full, `context.disconnect(…).await` in `notify` blocks, stalling the entire `notify` loop and preventing other protocol control messages (open/close protocol, send message) from being enqueued — effectively freezing protocol-level operations for all peers.

### Likelihood Explanation
The attacker only needs to:
1. Open `max_inbound` TCP connections to the target node's P2P port (no authentication required).
2. Complete the TCP handshake but never send any data on the Identify protocol stream.
3. Keep the TCP connection open (ignore or delay FIN/RST from the server side).

This is trivially achievable with a simple script. No PoW, no keys, no privileged access required.

### Recommendation
In `notify`, immediately mark or remove timed-out sessions to prevent re-processing on subsequent ticks. The minimal fix is to collect timed-out session IDs and remove them from `remote_infos` before (or immediately after) issuing the disconnect:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    let now = Instant::now();
    let timed_out: Vec<SessionId> = self.remote_infos
        .iter()
        .filter(|(_, info)| !info.has_received && (info.connected_at + info.timeout) <= now)
        .map(|(id, _)| *id)
        .collect();
    for session_id in timed_out {
        // Remove immediately so subsequent ticks do not re-fire
        if let Some(info) = self.remote_infos.remove(&session_id) {
            let misbehave_result = self.callback.misbehave(&info.session, Misbehavior::Timeout);
            if misbehave_result.is_disconnect() {
                let _ = context.disconnect(session_id).await;
            }
        }
    }
}
```

This makes the timeout detection idempotent: each session is disconnected at most once from `notify`, regardless of how long the transport takes to close.

### Proof of Concept
1. Start a CKB node with default config.
2. Open `max_inbound` TCP connections to its P2P port; complete the TCP handshake but send no data.
3. Keep the client sockets open (do not close them).
4. Wait 10 seconds (past `DEFAULT_TIMEOUT = 8`).
5. Instrument or log calls to `context.disconnect()` inside `notify`.
6. Observe that `disconnect` is called for each session on **every** 1-second tick — not just once — until the transport closes.
7. Assert: total `disconnect` calls > `N` (where `N` = number of attacker connections), confirming the storm.

### Citations

**File:** network/src/protocols/identify/mod.rs (L239-248)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.remote_infos
            .remove(&context.session.id)
            .expect("RemoteInfo must exists");
        debug!(
            "IdentifyProtocol disconnected, session: {:?}",
            context.session
        );
        self.callback.unregister(&context);
    }
```

**File:** network/src/protocols/identify/mod.rs (L316-325)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        for (session_id, info) in &self.remote_infos {
            if !info.has_received && (info.connected_at + info.timeout) <= Instant::now() {
                let misbehave_result = self.callback.misbehave(&info.session, Misbehavior::Timeout);
                if misbehave_result.is_disconnect() {
                    let _ = context.disconnect(*session_id).await;
                }
            }
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L509-514)
```rust
    fn misbehave(&mut self, session: &SessionContext, reason: Misbehavior) -> MisbehaveResult {
        error!(
            "IdentifyProtocol detects abnormal behavior, session: {:?}, reason: {:?}",
            session, reason
        );
        MisbehaveResult::Disconnect
```

**File:** util/app-config/src/configs/network.rs (L17-17)
```rust
const DEFAULT_CHANNEL_SIZE: usize = 1024;
```
