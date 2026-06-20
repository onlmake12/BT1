### Title
Expired Network Alerts Can Be Replayed by Any Unprivileged Peer — (`util/network-alert/src/notifier.rs`)

---

### Summary

`Notifier::clear_expired_alerts` evicts expired alerts from `received_alerts` but never records their IDs in `cancel_filter`. Because `has_received` is the sole replay gate and it checks only those two structures, any peer that saved a previously-broadcast, now-expired alert can re-inject it into the network. The node re-accepts the alert, re-fires `notify_network_alert`, and re-broadcasts it to all connected peers — no privileged key is required.

---

### Finding Description

**Vulnerability class:** Signed-message replay — direct analog of the external report's "no nonce / no invalidation tracking" pattern.

**Root cause — `clear_expired_alerts` does not tombstone expired IDs**

`Notifier` uses two structures to decide whether an alert has already been processed:

```
received_alerts : HashMap<u32, Alert>   // live alerts, keyed by id
cancel_filter   : LruCache<u32, ()>     // tombstones for explicitly cancelled ids
```

The deduplication gate is:

```rust
// util/network-alert/src/notifier.rs  line 147-149
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```

When an alert expires, `clear_expired_alerts` silently drops it from `received_alerts` **without writing its ID to `cancel_filter`**:

```rust
// util/network-alert/src/notifier.rs  lines 135-144
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now          // expired entries are simply dropped
    });
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
    // cancel_filter is never touched here
}
```

After this call, `has_received(id)` returns `false` for the expired alert.

**Attacker-controlled entry path — `AlertRelayer::received`**

Any P2P peer can send an `Alert` message. The handler checks `has_received`, then verifies the Nervos Foundation signature, then re-broadcasts and stores the alert — with **no expiry check at any step**:

```rust
// util/network-alert/src/alert_relayer.rs  lines 144-178
let alert_id = alert.as_reader().raw().id().into();
if self.notifier.lock().has_received(alert_id) {   // ← only gate; returns false after expiry
    return;
}
if let Err(err) = self.verifier.verify_signatures(&alert) {  // passes: sig is still valid
    nc.ban_peer(...);
    return;
}
// broadcast to all peers
nc.quick_filter_broadcast(..., data);
// store and notify
self.notifier.lock().add(&alert);
```

`Notifier::add` also has no expiry check:

```rust
// util/network-alert/src/notifier.rs  lines 93-122
pub fn add(&mut self, alert: &Alert) {
    if self.has_received(alert_id) { return; }   // false after expiry
    self.received_alerts.insert(alert_id, alert.clone());
    if !self.is_version_effective(alert) { return; }  // checks version only, not time
    self.notify_controller.notify_network_alert(alert.clone());  // ← fires again
    self.noticed_alerts.push(alert.clone());
}
```

`is_version_effective` checks only `min_version`/`max_version` ranges; it never inspects `notice_until`.

**Contrast with `cancel`:** When an alert is explicitly cancelled, `cancel()` writes the ID to `cancel_filter`, permanently blocking replay. Expiry has no equivalent tombstone.

---

### Impact Explanation

An unprivileged peer can force every connected node to:

1. Re-accept a previously-expired, legitimately-signed alert.
2. Re-fire `notify_network_alert`, surfacing the stale warning to node operators and any downstream subscribers (e.g., monitoring hooks, RPC clients polling `get_blockchain_info`).
3. Re-broadcast the alert to all of their peers, propagating the replay network-wide.

A past alert warning about a critical bug in a specific version range would reappear as an active warning, potentially causing operators to take unnecessary emergency actions (emergency upgrades, halting services, alarming users). The attack is repeatable: once the re-injected alert expires again, the attacker can replay it again indefinitely.

---

### Likelihood Explanation

- **No privileged key required.** The attacker only needs the raw bytes of a previously-broadcast alert, which any peer on the network can observe and store.
- **Trigger condition is automatic.** `clear_expired_alerts` is called on every new peer connection (`alert_relayer.rs` line 87), so the window opens naturally as the network operates.
- **Replayable indefinitely.** Each replay re-inserts the alert into `received_alerts`; after the next `clear_expired_alerts` call it is evicted again, re-opening the window.
- **Zero cost.** Sending a single P2P `Alert` message is the entire attack.

---

### Recommendation

In `clear_expired_alerts`, tombstone each evicted ID into `cancel_filter` before dropping it:

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
        self.cancel_filter.put(id, ());   // ← prevent replay
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Alternatively, add an expiry check at the top of `Notifier::add` and in `AlertRelayer::received` so that an alert whose `notice_until` is already in the past is rejected immediately.

---

### Proof of Concept

```
T=0   Nervos Foundation broadcasts Alert{id=42, notice_until=T+3600, message="critical bug"}
      → All nodes accept it; received_alerts[42] = alert

T=0   Attacker (any peer) captures and stores the raw alert bytes.

T=3601  A new peer connects to victim node.
        AlertRelayer::connected() calls clear_expired_alerts(now=T+3601).
        received_alerts.retain(...) drops id=42 (notice_until <= now).
        cancel_filter is NOT updated.
        has_received(42) now returns false.

T=3602  Attacker sends the saved alert bytes to victim node via P2P.
        AlertRelayer::received():
          has_received(42)          → false  (gate bypassed)
          verify_signatures(alert)  → Ok     (Nervos Foundation sig still valid)
          quick_filter_broadcast()  → alert re-propagated to all peers
          notifier.add(alert):
            received_alerts[42] = alert  (re-inserted)
            notify_network_alert(alert)  (user notified again)

T=3603  Every peer that received the replay also re-broadcasts it.
        Network-wide re-propagation of the expired alert.

T=3604  Attacker can repeat from T=3601 on every subsequent expiry cycle.
```

**Relevant code locations:**

- Root cause: [1](#0-0) 
- Deduplication gate: [2](#0-1) 
- No expiry check in `add`: [3](#0-2) 
- Entry point — `AlertRelayer::received`: [4](#0-3) 
- `clear_expired_alerts` triggered on peer connect: [5](#0-4) 
- `cancel` correctly tombstones (contrast): [6](#0-5)

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
