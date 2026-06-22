### Title
Expired Network Alert Replay via P2P — (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network alert system uses Foundation multi-signatures to authorize alerts broadcast over P2P. When an alert expires, `clear_expired_alerts` removes it from `received_alerts` without adding its ID to `cancel_filter`. This causes `has_received` to return `false` for the expired alert ID, allowing any peer who retained the original signed alert bytes to replay them indefinitely. The P2P `received` handler has no `notice_until` expiry check, so the replayed alert passes signature verification, re-triggers user notifications, and is re-broadcast to all connected peers.

---

### Finding Description

**Root cause — `clear_expired_alerts` does not invalidate expired IDs:**

In `util/network-alert/src/notifier.rs`, `clear_expired_alerts` only removes entries from `received_alerts` and `noticed_alerts`: [1](#0-0) 

It never writes the expired alert ID into `cancel_filter`. The deduplication gate is: [2](#0-1) 

After `clear_expired_alerts` runs, `has_received(id)` returns `false` for the now-expired ID because it is absent from both `received_alerts` and `cancel_filter`. The `cancel_filter` is only populated by explicit `cancel()` calls: [3](#0-2) 

**P2P handler has no expiry check:**

In `util/network-alert/src/alert_relayer.rs`, the `received` handler checks only `has_received` and signature validity — there is no `notice_until < now` rejection: [4](#0-3) 

`clear_expired_alerts` is triggered only on peer connection events: [5](#0-4) 

**Exploit flow:**

1. Foundation broadcasts a signed alert (ID=X, `notice_until`=T). Every connected node stores it in `received_alerts`.
2. The signed alert bytes are publicly visible on the P2P network; any peer can retain them.
3. After time T, an attacker connects to a victim node. This triggers `clear_expired_alerts`, which removes ID=X from `received_alerts` — but not from `cancel_filter`.
4. The attacker immediately sends the original signed alert bytes to the victim over the alert P2P protocol.
5. `has_received(X)` returns `false`; `verify_signatures` passes (signatures are unchanged and valid).
6. `notifier.add(&alert)` is called: [6](#0-5) 

7. `notify_controller.notify_network_alert` fires, re-displaying the expired alert to the node operator.
8. The alert is re-broadcast to all connected peers, propagating across the network.

The `cancel_filter` LRU capacity is only 128 entries: [7](#0-6) 

Even legitimately cancelled alerts can be evicted from `cancel_filter` after 128 subsequent cancellations, opening a second replay window for cancelled alerts — though this path requires Foundation key activity.

---

### Impact Explanation

An unprivileged attacker who observed the original alert on the P2P network can, after expiry:

- Force any reachable node to re-display an expired Foundation-signed alert to its operator via `notify_network_alert`.
- Cause the node to re-broadcast the alert to all its peers, propagating the replay network-wide.
- Repeat this indefinitely because `clear_expired_alerts` never permanently invalidates the ID.

The most realistic harm is social engineering: replaying a past "critical vulnerability — upgrade immediately" alert after the crisis has passed to induce unnecessary operator action or confusion. Because the signed bytes are authentic Foundation signatures, operators have no cryptographic way to distinguish a replay from a new alert with the same ID.

---

### Likelihood Explanation

- **Preconditions**: The attacker only needs to have received the alert bytes during the original broadcast (or obtain them from any peer). No privileged keys, no special access.
- **Trigger**: Connect to a target node (triggering `clear_expired_alerts`) then immediately send the retained alert bytes. This is a standard P2P connection — no special capability required.
- **Repeatability**: The attack can be repeated every time `clear_expired_alerts` is triggered on the target, because the ID is never permanently recorded.

Likelihood is **medium**: the attacker capability is minimal (any past network observer), but the attack window requires timing the replay after expiry cleanup.

---

### Recommendation

In `clear_expired_alerts`, record each expired alert ID into `cancel_filter` before removing it from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until <= now {
            self.cancel_filter.put(*id, ()); // permanently invalidate
            false
        } else {
            true
        }
    });
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, add an expiry check in the P2P `received` handler before accepting any alert:

```rust
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until <= ckb_systemtime::unix_time_as_millis() {
    // silently drop or disconnect peer
    return;
}
```

Increasing `CANCEL_FILTER_SIZE` beyond 128 also reduces the LRU-eviction replay window for cancelled alerts.

---

### Proof of Concept

```
1. Observe the P2P network and capture the raw bytes of a live Foundation alert
   (ID=42, notice_until = now + 86_400_000 ms, valid 2-of-4 signatures).

2. Wait until notice_until has passed.

3. Connect to a victim CKB node as a peer on the alert protocol.
   → This triggers AlertRelayer::connected → clear_expired_alerts,
     removing ID=42 from received_alerts.

4. Immediately send the captured alert bytes to the victim node.
   → has_received(42) == false  (not in received_alerts, not in cancel_filter)
   → verify_signatures passes   (signatures are authentic)
   → notifier.add() fires notify_network_alert and re-populates noticed_alerts
   → alert is re-broadcast to all of the victim's peers

5. Repeat from step 3 on any node at any time after expiry.
```

### Citations

**File:** util/network-alert/src/notifier.rs (L9-9)
```rust
const CANCEL_FILTER_SIZE: usize = 128;
```

**File:** util/network-alert/src/notifier.rs (L93-122)
```rust
    pub fn add(&mut self, alert: &Alert) {
        let alert_id = alert.raw().id().into();
        let alert_cancel = alert.raw().cancel().into();
        if self.has_received(alert_id) {
            return;
        }
        // checkout cancel_id
        if alert_cancel > 0 {
            self.cancel(alert_cancel);
        }
        // add to received alerts
        self.received_alerts.insert(alert_id, alert.clone());

        // check conditions, figure out do we need to notice this alert
        if !self.is_version_effective(alert) {
            debug!("Received a version ineffective alert {:?}", alert);
            return;
        }

        if self.noticed_alerts.contains(alert) {
            return;
        }
        self.notify_controller.notify_network_alert(alert.clone());
        self.noticed_alerts.push(alert.clone());
        // sort by priority
        self.noticed_alerts.sort_by_key(|a| {
            let priority: u32 = a.raw().priority().into();
            u32::MAX - priority
        });
    }
```

**File:** util/network-alert/src/notifier.rs (L125-132)
```rust
    pub fn cancel(&mut self, cancel_id: u32) {
        self.cancel_filter.put(cancel_id, ());
        self.received_alerts.remove(&cancel_id);
        self.noticed_alerts.retain(|a| {
            let id: u32 = a.raw().id().into();
            id != cancel_id
        });
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

**File:** util/network-alert/src/alert_relayer.rs (L144-178)
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
        // mark sender as known
        self.mark_as_known(peer_index, alert_id);
        // broadcast message
        let selected_peers: Vec<PeerIndex> = nc
            .connected_peers()
            .into_iter()
            .filter(|peer| self.mark_as_known(*peer, alert_id))
            .collect();
        if let Err(err) = nc.quick_filter_broadcast(
            TargetSession::Multi(Box::new(selected_peers.into_iter())),
            data,
        ) {
            debug!("alert broadcast error: {:?}", err);
        }
        // add to received alerts
        self.notifier.lock().add(&alert);
```
