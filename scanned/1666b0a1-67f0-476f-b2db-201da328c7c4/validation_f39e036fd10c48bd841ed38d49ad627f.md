### Title
Expired Network Alert Signature Replay via P2P — (`util/network-alert/src/alert_relayer.rs`, `util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert P2P handler (`AlertRelayer::received`) does not validate whether a received alert's `notice_until` timestamp is in the past. The deduplication guard (`has_received`) relies solely on the in-memory `received_alerts` HashMap, which is purged by `clear_expired_alerts()` on every new peer connection. After an alert expires and is cleared, any unprivileged P2P peer that retained the original alert bytes can replay them verbatim, bypassing all deduplication, passing signature verification, and causing the expired alert to be re-added to `noticed_alerts` and re-broadcast to all connected peers.

---

### Finding Description

**Root cause — missing `notice_until` check in the P2P receive path**

The RPC entry point `send_alert` correctly rejects stale alerts:

```rust
// rpc/src/module/alert.rs:104-110
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [1](#0-0) 

The P2P handler `AlertRelayer::received` performs no equivalent check. It only tests `has_received(alert_id)` and then verifies signatures:

```rust
// util/network-alert/src/alert_relayer.rs:144-162
let alert_id = alert.as_reader().raw().id().into();
if self.notifier.lock().has_received(alert_id) {
    return;
}
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
``` [2](#0-1) 

**Deduplication state is erased on every new peer connection**

`clear_expired_alerts()` is called inside `connected()`, which fires whenever any peer connects:

```rust
// util/network-alert/src/alert_relayer.rs:87
self.clear_expired_alerts();
``` [3](#0-2) 

`clear_expired_alerts()` removes the alert from both `received_alerts` and `noticed_alerts`:

```rust
// util/network-alert/src/notifier.rs:135-144
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| { ... });
}
``` [4](#0-3) 

After this purge, `has_received(id)` returns `false` for the expired alert ID (unless it was cancelled and still in the 128-entry LRU `cancel_filter`):

```rust
// util/network-alert/src/notifier.rs:147-149
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [5](#0-4) 

**Exploit flow**

1. A legitimate alert (ID=X, `notice_until`=T, valid 2-of-4 signatures) is broadcast and received by all nodes. Every node that received it stores the raw bytes.
2. Time advances past T. The next peer connection to any node triggers `clear_expired_alerts()`, removing alert X from `received_alerts`.
3. An attacker (any node that stored the original bytes) opens a P2P connection and sends the identical alert bytes.
4. `has_received(X)` → `false` (cleared). Signature verification passes (signatures are unchanged and still cryptographically valid). The alert is re-inserted into `noticed_alerts` and re-broadcast to all connected peers.
5. Every downstream node repeats step 4 because they also cleared the alert.

The attacker needs no private keys — only the original alert bytes, which were broadcast to all peers during the original dissemination.

---

### Impact Explanation

- **Stale alert re-display**: Node operators and monitoring systems see a previously-expired critical alert re-appear, potentially triggering false incident responses (e.g., emergency upgrades for a bug that was already patched).
- **Network-wide re-broadcast**: The replayed alert propagates to all connected peers, amplifying the effect across the entire network. Each node that cleared the alert will re-accept and re-forward it.
- **Repeated triggering**: The attack can be repeated indefinitely — every time a peer connects (triggering `clear_expired_alerts`) and the attacker replays the bytes, the cycle restarts.

---

### Likelihood Explanation

- **Preconditions**: The attacker must have received the original alert bytes (trivially satisfied for any node that was online during the original broadcast) and must wait for the alert to expire and be cleared.
- **No privileged access required**: No signing keys are needed. The attack reuses the original, legitimately-signed bytes.
- **Trigger is automatic**: `clear_expired_alerts()` fires on every new peer connection, so the deduplication window resets frequently in a live network.
- **Realistic**: Any node operator or passive observer who archived alert traffic can execute this.

---

### Recommendation

Add a `notice_until` expiry check in the P2P `received()` handler, mirroring the check already present in the RPC handler:

```rust
// In AlertRelayer::received(), after parsing the alert:
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // silently drop or disconnect peer
    return;
}
```

This ensures that even after `received_alerts` is cleared, replayed expired alerts are rejected at the P2P boundary before any deduplication or signature check is reached.

---

### Proof of Concept

```
Setup:
  - Node A and Node B are connected.
  - Nervos Foundation broadcasts Alert(id=42, notice_until=T, msg="Upgrade now!", sigs=[s1,s2]).
  - Both nodes receive it; Node B stores the raw bytes.

Step 1 (T passes):
  - Node A's clear_expired_alerts() fires (triggered by any new peer connection).
  - Alert id=42 is removed from received_alerts.
  - has_received(42) → false.

Step 2 (replay):
  - Node B opens a P2P connection to Node A and sends the original alert bytes.
  - alert_relayer::received() on Node A:
      has_received(42)          → false  ✓ (passes dedup check)
      notice_until check        → absent ✗ (no check in P2P path)
      verify_signatures(&alert) → Ok(())  ✓ (signatures still valid)
  - Alert re-added to noticed_alerts; re-broadcast to all of Node A's peers.

Step 3 (propagation):
  - All peers of Node A that also cleared the alert repeat the same acceptance.
  - The expired alert propagates network-wide again.
```

### Citations

**File:** rpc/src/module/alert.rs (L104-110)
```rust
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until < now_ms {
            return Err(RPCError::invalid_params(format!(
                "Expected `params[0].notice_until` in the future (> {now_ms}), got {notice_until}",
            )));
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L86-94)
```rust
    ) {
        self.clear_expired_alerts();
        for alert in self.notifier.lock().received_alerts() {
            let alert_id: u32 = alert.as_reader().raw().id().into();
            trace!("Send alert {} to peer {}", alert_id, peer_index);
            if let Err(err) = nc.quick_send_message_to(peer_index, alert.as_bytes()) {
                debug!("alert_relayer send alert when connected error: {:?}", err);
            }
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L144-162)
```rust
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
        // verify
        if let Err(err) = self.verifier.verify_signatures(&alert) {
            debug!(
                "An alert from peer {} with invalid signatures, error {:?}",
                peer_index, err
            );
            nc.ban_peer(
                peer_index,
                BAD_MESSAGE_BAN_TIME,
                String::from("send us an alert with invalid signatures"),
            );
            return;
        }
```

**File:** util/network-alert/src/notifier.rs (L135-144)
```rust
    pub fn clear_expired_alerts(&mut self, now: u64) {
        self.received_alerts.retain(|_id, alert| {
            let notice_until: u64 = alert.raw().notice_until().into();
            notice_until > now
        });
        self.noticed_alerts.retain(|a| {
            let notice_until: u64 = a.raw().notice_until().into();
            notice_until > now
        });
    }
```

**File:** util/network-alert/src/notifier.rs (L147-149)
```rust
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
    }
```
