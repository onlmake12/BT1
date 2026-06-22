### Title
Expired Network Alert Replay via Missing Invalidation in `clear_expired_alerts` - (File: `util/network-alert/src/notifier.rs`)

---

### Summary

The `Notifier::clear_expired_alerts` function removes expired alerts from `received_alerts` without recording their IDs in `cancel_filter`. Because `has_received` is the sole deduplication gate in the P2P alert receive path, and because `add` performs no `notice_until > now` check, any unprivileged peer that stored a previously-broadcast alert can replay it after it expires. The replayed alert passes signature verification (the original Nervos Foundation signatures are still valid), is accepted as new, triggers `notify_network_alert`, and is re-broadcast to all connected peers.

---

### Finding Description

The `Notifier` struct in `util/network-alert/src/notifier.rs` maintains two data structures for deduplication:

- `received_alerts: HashMap<u32, Alert>` — alerts currently held in memory
- `cancel_filter: LruCache<u32, ()>` — IDs of explicitly cancelled alerts

`has_received` checks both:

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [1](#0-0) 

When an alert expires, `clear_expired_alerts` removes it from `received_alerts` and `noticed_alerts`, but **never inserts its ID into `cancel_filter`**:

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

After this call, `has_received(id)` returns `false` for the expired alert's ID. The P2P receive handler in `alert_relayer.rs` uses `has_received` as its only replay guard:

```rust
if self.notifier.lock().has_received(alert_id) {
    return;
}
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
// ...
self.notifier.lock().add(&alert);
``` [3](#0-2) 

`Notifier::add` also performs no `notice_until > now` check before inserting into `received_alerts`, calling `notify_network_alert`, and pushing to `noticed_alerts`: [4](#0-3) 

`clear_expired_alerts` is triggered on every peer connection event and on every `get_blockchain_info` RPC call, so the window opens reliably after the alert's `notice_until` timestamp passes. [5](#0-4) 

---

### Impact Explanation

An unprivileged P2P peer that stored a previously-broadcast alert can, after the alert's `notice_until` timestamp passes:

1. Re-send the original alert bytes to any connected node.
2. The node's `has_received` check passes (ID no longer in either map).
3. Signature verification passes (the original Nervos Foundation signatures are cryptographically valid forever).
4. The node calls `notify_network_alert`, surfacing the expired alert to operators and RPC clients.
5. The node re-broadcasts the alert to all its peers, propagating the replay network-wide.

If the replayed alert carries a non-zero `cancel` field, it also silently cancels whichever currently-active alert that field references, potentially suppressing a live critical warning.

---

### Likelihood Explanation

- **Attacker capability required**: none beyond being a connected P2P peer and having stored the alert message (which was broadcast to the entire network).
- **Trigger condition**: wait for `notice_until` to pass and for any node to call `clear_expired_alerts` (happens automatically on peer connect or `get_blockchain_info`).
- **No key material needed**: the original Nervos Foundation signatures embedded in the alert are reused verbatim.
- **Realistic**: alerts are retained by any node that received them; the attack is passive storage followed by a single P2P message.

---

### Recommendation

In `clear_expired_alerts`, insert each expiring alert's ID into `cancel_filter` before dropping it from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    let expired_ids: Vec<u32> = self.received_alerts
        .iter()
        .filter(|(_, alert)| {
            let notice_until: u64 = alert.raw().notice_until().into();
            notice_until <= now
        })
        .map(|(id, _)| *id)
        .collect();
    for id in expired_ids {
        self.received_alerts.remove(&id);
        self.cancel_filter.put(id, ()); // prevent replay
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, add an expiry check in `Notifier::add` and in the `received` handler of `AlertRelayer` to reject alerts whose `notice_until` is already in the past, providing defense-in-depth.

Consider also increasing `CANCEL_FILTER_SIZE` beyond 128 to prevent LRU eviction from creating a second replay window for explicitly cancelled alerts. [6](#0-5) 

---

### Proof of Concept

1. Connect a node to the CKB network and receive a legitimate alert with `id=42`, `notice_until=T`.
2. Store the raw alert bytes.
3. Wait until `T` passes. Any peer connection to the victim node (or a `get_blockchain_info` RPC call) triggers `clear_expired_alerts`, removing ID 42 from `received_alerts`.
4. Send the stored alert bytes to the victim node over the P2P alert protocol.
5. The victim node's `has_received(42)` returns `false`; signature verification passes; `notify_network_alert` fires; the alert appears in `get_blockchain_info().alerts`; the node re-broadcasts the alert to all its peers.

The attack requires no privileged keys, no majority hashpower, and no social engineering — only a stored P2P message and a TCP connection.

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

**File:** util/network-alert/src/alert_relayer.rs (L58-61)
```rust
    fn clear_expired_alerts(&mut self) {
        let now = ckb_systemtime::unix_time_as_millis();
        self.notifier.lock().clear_expired_alerts(now);
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L147-178)
```rust
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
