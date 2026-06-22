### Title
Expired Alert Signature Replay via P2P — (`util/network-alert/src/alert_relayer.rs`, `util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system deduplicates received alerts by `alert_id` in an in-memory map (`received_alerts`). When `clear_expired_alerts` is called, expired alerts are evicted from `received_alerts` without being added to the `cancel_filter`. Because the P2P `received` handler does not independently check `notice_until` against the current time, any unprivileged P2P peer can replay the raw bytes of a previously-expired, legitimately-signed alert. The node re-accepts it, re-notifies users, and re-broadcasts it to all connected peers.

---

### Finding Description

**Signed message has no replay protection beyond in-memory deduplication that is periodically cleared.**

The `RawAlert` molecule struct is:

```
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
}
``` [1](#0-0) 

The `Verifier::verify_signatures` signs and verifies only the hash of `RawAlert` — there is no nonce, no chain-id, and no per-node context in the signed payload. [2](#0-1) 

The P2P `received` handler in `AlertRelayer` guards against re-processing using `has_received(alert_id)`: [3](#0-2) 

`has_received` checks two structures: `received_alerts` (a `HashMap`) and `cancel_filter` (an `LruCache`): [4](#0-3) 

`clear_expired_alerts`, triggered on every new peer connection, evicts expired alerts from `received_alerts` and `noticed_alerts` — but **does not insert the evicted IDs into `cancel_filter`**: [5](#0-4) 

After eviction, `has_received(id)` returns `false` for the expired alert's ID. The P2P handler then proceeds to full signature verification (which passes, because the bytes are identical to the original valid alert), re-adds the alert to `received_alerts`, calls `notify_controller.notify_network_alert`, and re-broadcasts to all connected peers: [6](#0-5) 

The RPC `send_alert` path does check `notice_until < now_ms` and rejects expired alerts: [7](#0-6) 

But the P2P `received` path has no equivalent check, creating an asymmetry that enables replay.

---

### Impact Explanation

An unprivileged P2P peer who observed a past legitimate alert (e.g., a "critical bug" warning that has since expired) can replay its raw bytes at any time after the alert has been cleared by `clear_expired_alerts`. Every receiving node will:

1. Re-accept the alert as new (signature check passes, dedup check passes).
2. Re-notify users via `notify_controller.notify_network_alert` — surfacing a stale, expired emergency message.
3. Re-broadcast the alert to all connected peers, propagating the replay network-wide.

If the replayed alert contained a `cancel` field targeting a currently-active alert, the replay would also silently cancel that active alert on every node that processes it, suppressing a live warning.

---

### Likelihood Explanation

- Any P2P peer (no privilege required) can send arbitrary alert bytes over the Alert protocol.
- Alerts are broadcast to all nodes, so any observer on the network can capture the raw bytes.
- `clear_expired_alerts` is called on every new peer connection, so the dedup state resets regularly in a live network.
- The attacker only needs to wait for an alert to expire, then connect to a target node and send the saved bytes.

---

### Recommendation

1. **In `clear_expired_alerts`**: insert evicted alert IDs into `cancel_filter` before removing them from `received_alerts`, so `has_received` continues to return `true` for expired IDs.

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
        self.cancel_filter.put(id, ()); // prevent replay
        self.received_alerts.remove(&id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

2. **In the P2P `received` handler**: add an explicit `notice_until` check mirroring the RPC path, rejecting alerts whose expiry is already in the past before even verifying signatures.

---

### Proof of Concept

1. Observe a legitimate alert (id=42, notice_until=T) broadcast across the network. Save its raw bytes.
2. Wait until time T passes (alert expires).
3. Connect to any CKB node as a P2P peer on the Alert protocol.
4. The node calls `clear_expired_alerts` on connection, evicting id=42 from `received_alerts` without adding it to `cancel_filter`.
5. Send the saved raw alert bytes.
6. The node's `received` handler calls `has_received(42)` → `false` (evicted, not in cancel_filter).
7. `verify_signatures` passes (original valid signatures).
8. `notifier.add(&alert)` re-inserts id=42, calls `notify_network_alert`, and the node re-broadcasts to all peers.
9. The expired "critical bug" alert is now displayed again on every node in the network. [8](#0-7) [9](#0-8)

### Citations

**File:** util/gen-types/schemas/extensions.mol (L445-458)
```text
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
}

table Alert {
    raw:                        RawAlert,
    signatures:                 BytesVec,
}
```

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
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

**File:** util/network-alert/src/alert_relayer.rs (L144-149)
```rust
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L150-179)
```rust
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

**File:** util/network-alert/src/notifier.rs (L134-144)
```rust
    /// Clear all expired alerts
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
