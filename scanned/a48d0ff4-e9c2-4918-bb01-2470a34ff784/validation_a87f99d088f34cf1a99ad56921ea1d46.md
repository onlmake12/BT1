### Title
Expired Alert ID Eviction from Deduplication Set Enables Replay of Signed Alerts via P2P — (`util/network-alert/src/notifier.rs`)

---

### Summary

The `Notifier::clear_expired_alerts()` function removes expired alerts from `received_alerts` and `noticed_alerts` but does **not** add their IDs to `cancel_filter`. Because `has_received()` checks only those two structures, an expired alert's ID is no longer tracked after cleanup. Any unprivileged P2P peer that observed the original alert can replay it verbatim — with its original valid signatures — and every receiving node will re-accept, re-store, and re-broadcast it as if it were a new alert.

---

### Finding Description

**Root cause — `clear_expired_alerts` does not persist the ID:**

```
// util/network-alert/src/notifier.rs  lines 135-144
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

The alert ID is silently dropped from both maps. `cancel_filter` is never touched.

**Deduplication check — only covers live and explicitly cancelled IDs:**

```
// util/network-alert/src/notifier.rs  lines 147-149
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```

After `clear_expired_alerts` runs, `has_received(expired_id)` returns `false`.

**P2P receive path — no `notice_until` guard:**

```
// util/network-alert/src/alert_relayer.rs  lines 144-162
let alert_id = alert.as_reader().raw().id().into();
// ignore alert
if self.notifier.lock().has_received(alert_id) {   // ← returns false for expired IDs
    return;
}
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) {  // ← passes: sigs are genuine
    ...
}
// add to received alerts
self.notifier.lock().add(&alert);   // ← re-accepted
```

The RPC `send_alert` handler **does** reject expired alerts:

```
// rpc/src/module/alert.rs  lines 104-110
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
```

But the P2P `received()` handler has no equivalent check, so the guard is bypassed entirely when the replay arrives over the wire.

**Trigger for cleanup:** `clear_expired_alerts` is called inside `connected()` on every new peer connection, so the window opens naturally as the network grows.

```
// util/network-alert/src/alert_relayer.rs  lines 81-95
async fn connected(...) {
    self.clear_expired_alerts();
    ...
}
```

**End-to-end exploit flow:**

1. Nervos Foundation broadcasts a legitimate alert (e.g., `id=42`, `notice_until=T`, valid 2-of-4 sigs). Every node stores it.
2. Any peer (attacker) records the raw bytes of the alert message.
3. Time advances past `T`. A new peer connects to a victim node → `clear_expired_alerts()` fires → alert `42` is removed from `received_alerts`; `cancel_filter` is untouched.
4. `has_received(42)` now returns `false` on the victim node.
5. Attacker sends the original raw alert bytes to the victim via the Alert P2P protocol.
6. Victim's `received()` handler: `has_received(42)` → `false`; `verify_signatures` → `Ok` (original sigs are valid); `notifier.add(&alert)` → alert re-inserted into `received_alerts` and `noticed_alerts`; alert re-broadcast to all connected peers.
7. All reachable nodes re-display the stale alert to operators.

---

### Impact Explanation

An unprivileged P2P peer can force every reachable CKB node to re-display an expired, previously-cancelled, or superseded alert as if it were a current, active warning. Because the alert system is the primary out-of-band channel for communicating critical network events (e.g., "upgrade immediately due to consensus bug"), replaying a stale alert can:

- Cause node operators to take unnecessary emergency actions (forced upgrades, halting operations).
- Suppress awareness of a real current alert by flooding the `noticed_alerts` list with replayed noise.
- Undermine operator trust in the alert system over time (cry-wolf effect).

The impact is confined to the informational/operational layer; there is no direct financial loss or consensus state corruption.

---

### Likelihood Explanation

The attacker preconditions are minimal: establish a P2P connection to any CKB node (no authentication required) and have previously observed any alert that has since expired. Alerts are broadcast to all peers, so any node that was online during the original broadcast has the necessary bytes. The cleanup trigger (`connected()`) fires automatically, so the window opens without any attacker action. Likelihood is **medium**: requires patience (waiting for expiry) but zero cryptographic capability.

---

### Recommendation

In `clear_expired_alerts`, add expired IDs to `cancel_filter` before removing them from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    let expired_ids: Vec<u32> = self.received_alerts
        .iter()
        .filter_map(|(id, alert)| {
            let notice_until: u64 = alert.raw().notice_until().into();
            if notice_until <= now { Some(*id) } else { None }
        })
        .collect();
    for id in expired_ids {
        self.cancel_filter.put(id, ());   // ← persist the ID
        self.received_alerts.remove(&id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, add a `notice_until > now` guard at the top of the P2P `received()` handler (mirroring the RPC guard) as defence-in-depth.

---

### Proof of Concept

```
// Pseudocode
1. Connect to any CKB mainnet node as a normal P2P peer.
2. Capture the raw bytes of any alert message relayed to you
   (e.g., the real alert id=20230001 from 2023).
3. Wait until notice_until has passed AND a new peer connects to
   the target node (triggering clear_expired_alerts).
4. Send the captured raw bytes to the target node via the Alert
   protocol (protocol_id = SupportProtocols::Alert).
5. Observe: the target node re-accepts the alert, re-broadcasts
   it to its peers, and re-displays it in get_blockchain_info().alerts.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/network-alert/src/notifier.rs (L9-14)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
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

**File:** util/network-alert/src/alert_relayer.rs (L81-95)
```rust
    async fn connected(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        _version: &str,
    ) {
        self.clear_expired_alerts();
        for alert in self.notifier.lock().received_alerts() {
            let alert_id: u32 = alert.as_reader().raw().id().into();
            trace!("Send alert {} to peer {}", alert_id, peer_index);
            if let Err(err) = nc.quick_send_message_to(peer_index, alert.as_bytes()) {
                debug!("alert_relayer send alert when connected error: {:?}", err);
            }
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
