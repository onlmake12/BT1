### Title
Expired Alert ID Not Permanently Consumed — Re-Relay of Expired Signed Alerts by Any Peer - (File: `util/network-alert/src/notifier.rs`)

### Summary

`clear_expired_alerts` removes expired alerts from `received_alerts` but never records their IDs in `cancel_filter`. After expiry, `has_received(alert_id)` returns `false`, so any peer that retained a copy of a previously valid, fully-signed alert can replay it. The node re-accepts it, re-notifies subscribers, and re-broadcasts it network-wide — identical in structure to the `signId`-not-consumed pattern in the reference report.

### Finding Description

`Notifier` tracks two deduplication structures:

- `received_alerts: HashMap<u32, Alert>` — live alerts keyed by `alert_id`
- `cancel_filter: LruCache<u32, ()>` — permanently cancelled IDs

`has_received` gates every incoming alert:

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [1](#0-0) 

When `clear_expired_alerts` fires (triggered on every new peer connection), it silently drops expired entries from `received_alerts` and `noticed_alerts` without writing the IDs to `cancel_filter`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| { ... });
}
``` [2](#0-1) 

Compare with `cancel`, which correctly writes to `cancel_filter`:

```rust
pub fn cancel(&mut self, cancel_id: u32) {
    self.cancel_filter.put(cancel_id, ());
    self.received_alerts.remove(&cancel_id);
    ...
}
``` [3](#0-2) 

After expiry, `has_received(alert_id)` returns `false`. In `alert_relayer.rs`, the relay handler checks `has_received` before signature verification:

```rust
if self.notifier.lock().has_received(alert_id) {
    return;
}
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
// broadcast message
...
// add to received alerts
self.notifier.lock().add(&alert);
``` [4](#0-3) 

Because the signatures on the original alert are still cryptographically valid (they cover the alert content, not a timestamp), the replayed alert passes `verify_signatures`, is re-inserted into `received_alerts` and `noticed_alerts`, triggers `notify_network_alert` again, and is re-broadcast to all connected peers. [5](#0-4) 

### Impact Explanation

Any peer that retained a copy of a legitimately-signed, now-expired alert can:

1. Reconnect (triggering `clear_expired_alerts` on the target node, clearing the deduplication state).
2. Send the stored alert bytes.
3. Cause the target node to re-accept, re-display, and re-broadcast the alert to every connected peer.

This produces network-wide re-propagation of stale alerts, repeated `notify_network_alert` callbacks to all subscribers (RPC subscription clients, internal services), and display of expired emergency messages to node operators — potentially causing confusion or masking a real current alert with an outdated one. The re-broadcast fans out to all peers, multiplying the effect.

### Likelihood Explanation

Every node that was online during the original alert broadcast received and could have stored the raw signed bytes. No key material is needed; the attacker only needs to have observed the network during the alert's active window. The trigger condition (`clear_expired_alerts`) fires automatically on every new peer connection, making the deduplication gap easy to hit. The attack requires no special privilege, no majority hashpower, and no social engineering.

### Recommendation

In `clear_expired_alerts`, record each removed alert's ID into `cancel_filter` before dropping it from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until <= now {
            self.cancel_filter.put(*id, ());
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

This mirrors the existing `cancel` path and ensures expired IDs are permanently suppressed, consistent with how cancelled alerts are handled. [6](#0-5) 

### Proof of Concept

1. Nervos Foundation broadcasts a valid alert with `id = 42`, `notice_until = T`.
2. Attacker node receives and stores the raw signed alert bytes.
3. Time advances past `T`; the alert expires.
4. Attacker connects to a victim node. `clear_expired_alerts` fires, removing `id=42` from `received_alerts`. `cancel_filter` is untouched.
5. Attacker sends the stored alert bytes over the P2P alert protocol.
6. Victim calls `has_received(42)` → `false` (not in `received_alerts`, not in `cancel_filter`).
7. `verify_signatures` passes (signatures are still valid over the original content).
8. `notifier.add(&alert)` re-inserts `id=42` into `received_alerts` and `noticed_alerts`, calls `notify_network_alert`.
9. Victim broadcasts the alert to all its connected peers, who repeat the process. [7](#0-6) [8](#0-7)

### Citations

**File:** util/network-alert/src/notifier.rs (L9-21)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
    /// alerts we received
    received_alerts: HashMap<u32, Alert>,
    /// alerts that self node should notice
    noticed_alerts: Vec<Alert>,
    client_version: Option<Version>,
    notify_controller: NotifyController,
}
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

**File:** util/network-alert/src/verifier.rs (L33-64)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
        let signatures: Vec<Signature> = alert
            .signatures()
            .into_iter()
            .filter_map(
                |sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
                    Ok(sig) => {
                        if sig.is_valid() {
                            Some(sig)
                        } else {
                            debug!("invalid signature: {:?}", sig);
                            None
                        }
                    }
                    Err(err) => {
                        debug!("signature error: {}", err);
                        None
                    }
                },
            )
            .collect();
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
    }
```
