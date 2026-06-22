### Title
Alert Signature Replay via `send_alert` RPC Causes Unbounded Repeated P2P Broadcasts — (`rpc/src/module/alert.rs`)

---

### Summary

The `send_alert` RPC handler unconditionally broadcasts a valid alert to all connected peers on every call, with no check for whether the alert has already been received. Any local RPC caller who possesses a previously-broadcast, validly-signed alert can replay it repeatedly, causing the node to re-flood the P2P network with the same alert message until the alert's `notice_until` expiry.

---

### Finding Description

CKB's alert system uses a multi-signature scheme (2-of-4 Nervos Foundation keys) to authenticate network alerts. Once a valid alert is broadcast over P2P, its bytes — including the valid signatures — are publicly observable by any connected peer.

The P2P inbound path in `alert_relayer.rs` correctly guards against replay:

```rust
// ignore alert
if self.notifier.lock().has_received(alert_id) {
    return;   // ← early exit, no re-broadcast
}
``` [1](#0-0) 

The RPC path in `rpc/src/module/alert.rs` has no equivalent guard. After signature verification passes, it unconditionally calls `broadcast_with_handle`:

```rust
Ok(()) => {
    self.notifier.lock().add(&alert);          // deduplicates internally, returns ()
    self.network_controller.broadcast_with_handle(
        SupportProtocols::Alert.protocol_id(),
        alert.as_bytes(),
        &self.handle,
    );
    Ok(())
}
``` [2](#0-1) 

`notifier.add()` does deduplicate internally by `alert_id`:

```rust
pub fn add(&mut self, alert: &Alert) {
    let alert_id = alert.raw().id().into();
    if self.has_received(alert_id) {
        return;   // ← silent no-op, returns ()
    }
``` [3](#0-2) 

But because `add()` returns `()` in both the new-alert and duplicate-alert cases, the `send_alert` handler cannot distinguish them. The `broadcast_with_handle` call fires unconditionally on every RPC invocation that passes signature verification, regardless of whether the alert was already known.

The only time-bound protection is the `notice_until` expiry check at the top of `send_alert`:

```rust
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [4](#0-3) 

This mirrors the external report exactly: the signed message (alert) lacks a per-use nonce, so the same signature is valid for the entire lifetime of the alert (`notice_until`), and the RPC endpoint does not track whether it has already acted on that signature.

---

### Impact Explanation

An attacker with local RPC access can extract a valid signed alert from the P2P network and call `send_alert` in a tight loop. Each call causes the node to broadcast the alert bytes to all currently-connected peers. Downstream peers receiving the duplicate will drop it via their own `has_received` guard, but the attacked node still performs the broadcast work and consumes outbound bandwidth for every RPC call. This degrades the node's P2P performance and wastes bandwidth on all directly-connected peers for the duration of the alert's TTL.

---

### Likelihood Explanation

Valid alert data (including signatures) is broadcast in plaintext over the P2P network and is observable by any connected peer. The `send_alert` RPC is a documented, publicly-accessible endpoint. A local RPC user (a supported attacker profile per scope) can trivially replay a captured alert. No privileged key material is required — only possession of the already-broadcast alert bytes.

---

### Recommendation

Add a `has_received` check in `send_alert` before calling `broadcast_with_handle`, mirroring the guard already present in `alert_relayer.rs`:

```rust
let alert_id: u32 = alert.raw().id().into();
let mut notifier = self.notifier.lock();
if notifier.has_received(alert_id) {
    return Err(RPCError::invalid_params("alert already received"));
}
notifier.add(&alert);
drop(notifier);
self.network_controller.broadcast_with_handle(...);
```

Alternatively, change `notifier.add()` to return a `bool` indicating whether the alert was new, and only broadcast when `true`.

---

### Proof of Concept

1. Connect to a CKB node's P2P port and observe a valid alert broadcast (or obtain one from a prior `send_alert` call). The alert bytes include the valid Nervos Foundation signatures.
2. Decode the alert bytes into the JSON `Alert` structure (id, message, notice_until, signatures, etc.).
3. In a loop, call the `send_alert` RPC on the target node with the same alert JSON:
   ```bash
   while true; do
     curl -s -X POST http://127.0.0.1:8114 \
       -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","method":"send_alert","params":[<captured_alert_json>],"id":1}'
   done
   ```
4. Each call returns `null` (success), and the node broadcasts the alert to all connected peers on every iteration.
5. The loop is effective until `notice_until` expires, at which point the RPC returns `InvalidParams`.

### Citations

**File:** util/network-alert/src/alert_relayer.rs (L146-149)
```rust
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** rpc/src/module/alert.rs (L106-110)
```rust
        if notice_until < now_ms {
            return Err(RPCError::invalid_params(format!(
                "Expected `params[0].notice_until` in the future (> {now_ms}), got {notice_until}",
            )));
        }
```

**File:** rpc/src/module/alert.rs (L115-124)
```rust
            Ok(()) => {
                // set self node notifier
                self.notifier.lock().add(&alert);

                self.network_controller.broadcast_with_handle(
                    SupportProtocols::Alert.protocol_id(),
                    alert.as_bytes(),
                    &self.handle,
                );
                Ok(())
```

**File:** util/network-alert/src/notifier.rs (L93-98)
```rust
    pub fn add(&mut self, alert: &Alert) {
        let alert_id = alert.raw().id().into();
        let alert_cancel = alert.raw().cancel().into();
        if self.has_received(alert_id) {
            return;
        }
```
