### Title
Expired Alert Re-injection via `clear_expired_alerts` / `has_received` State Desynchronization - (File: `util/network-alert/src/notifier.rs`)

### Summary

`Notifier::clear_expired_alerts` removes expired alerts from `received_alerts` and `noticed_alerts` but never adds their IDs to `cancel_filter`. Because `has_received` checks only those two structures, it returns `false` for any previously-seen alert whose expiry has been cleaned up. Any P2P peer that retained the original (validly-signed) alert bytes can re-send them after expiry, bypass the dedup guard, and cause the node to re-accept, re-broadcast, and re-notify the expired alert.

### Finding Description

`Notifier` maintains three structures for alert deduplication:

| Field | Type | Purpose |
|---|---|---|
| `received_alerts` | `HashMap<u32, Alert>` | All alerts seen and not yet expired |
| `noticed_alerts` | `Vec<Alert>` | Alerts the node should display |
| `cancel_filter` | `LruCache<u32, ()>` | IDs of explicitly-cancelled alerts |

`has_received` is the sole dedup gate:

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [1](#0-0) 

`clear_expired_alerts` removes expired entries from both `received_alerts` and `noticed_alerts`, but **never inserts the expired IDs into `cancel_filter`**:

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
``` [2](#0-1) 

After this call, `has_received(expired_id)` returns `false` — the ID is absent from both `received_alerts` (just removed) and `cancel_filter` (never inserted). This is the direct analog of the original bug: an element is removed from one collection but the complementary state is not updated, making the membership check return the wrong answer.

`clear_expired_alerts` is triggered on every new peer connection:

```rust
fn clear_expired_alerts(&mut self) {
    let now = ckb_systemtime::unix_time_as_millis();
    self.notifier.lock().clear_expired_alerts(now);
}
``` [3](#0-2) 

and in `AlertRelayer::connected`: [4](#0-3) 

After cleanup, the `received` handler's dedup check is defeated:

```rust
if self.notifier.lock().has_received(alert_id) {
    return;
}
``` [5](#0-4) 

The re-injected alert then passes signature verification (the bytes are identical to the original valid alert), gets re-added to `received_alerts` and `noticed_alerts`, triggers `notify_controller.notify_network_alert`, and is re-broadcast to all connected peers: [6](#0-5) 

### Impact Explanation

- **Re-broadcast amplification**: Every node that cleans up an expired alert becomes susceptible. A single attacker peer re-sending the expired alert causes the victim node to re-broadcast it to all its peers, which may themselves be susceptible, creating a network-wide re-flood of expired alert traffic.
- **Re-notification**: `notify_controller.notify_network_alert` fires again, causing subscribers (e.g., RPC subscribers, internal services) to process the alert a second time.
- **Temporary state corruption**: The expired alert re-appears in `noticed_alerts` and `received_alerts` until the next `clear_expired_alerts` call.

### Likelihood Explanation

- Signed alert bytes are public — every node that received the original alert has them.
- Any ordinary P2P peer (no privilege required) can re-send the original bytes after the alert expires.
- `clear_expired_alerts` is called on every new peer connection, so the window opens automatically as time passes.
- The attack is trivially repeatable: the attacker reconnects, triggering cleanup, then re-sends the expired alert.

### Recommendation

In `clear_expired_alerts`, insert each removed alert's ID into `cancel_filter` before dropping it from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    let expired_ids: Vec<u32> = self.received_alerts
        .iter()
        .filter_map(|(id, alert)| {
            let notice_until: u64 = alert.raw().notice_until().into();
            if notice_until <= now { Some(*id) } else { None }
        })
        .collect();
    for id in &expired_ids {
        self.cancel_filter.put(*id, ());   // <-- add this
        self.received_alerts.remove(id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

This mirrors the correct fix in the original report: ensure that every removal path updates all relevant state so that the membership check (`has_received`) remains consistent.

### Proof of Concept

1. Node receives alert `id=42`, `notice_until=T`. It is stored in `received_alerts` and `noticed_alerts`. `has_received(42)` → `true`.
2. Time advances past `T`. A new peer connects → `AlertRelayer::connected` → `clear_expired_alerts(now > T)` → alert `42` removed from `received_alerts` and `noticed_alerts`. `cancel_filter` is **not** updated.
3. `has_received(42)` → `false` (absent from both `received_alerts` and `cancel_filter`).
4. Attacker peer re-sends the original alert `42` bytes (which carry valid Nervos Foundation signatures).
5. `AlertRelayer::received`: `has_received(42)` → `false` → dedup guard skipped. Signature verification passes (same bytes). Alert is re-added to `received_alerts` and `noticed_alerts`. `notify_controller.notify_network_alert` fires. Alert is re-broadcast to all connected peers.
6. Steps 2–5 repeat indefinitely as long as the attacker keeps reconnecting and re-sending. [7](#0-6) [8](#0-7)

### Citations

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

**File:** util/network-alert/src/notifier.rs (L135-149)
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

    /// Whether id received
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L58-61)
```rust
    fn clear_expired_alerts(&mut self) {
        let now = ckb_systemtime::unix_time_as_millis();
        self.notifier.lock().clear_expired_alerts(now);
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L81-94)
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
```

**File:** util/network-alert/src/alert_relayer.rs (L147-149)
```rust
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L163-179)
```rust
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
    }
```
