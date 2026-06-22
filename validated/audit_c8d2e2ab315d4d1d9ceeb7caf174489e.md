### Title
Expired Network Alert ID Replay via P2P — (`util/network-alert/src/alert_relayer.rs`, `util/network-alert/src/notifier.rs`)

---

### Summary

The CKB P2P alert handler (`AlertRelayer::received`) does not validate `notice_until` before accepting an inbound alert. The `Notifier` evicts expired alerts from `received_alerts` on `clear_expired_alerts()` without recording their IDs in `cancel_filter`, so `has_received()` returns `false` for any expired alert ID. An unprivileged P2P peer who captured a previously-broadcast, validly-signed alert can replay it after it expires, causing every receiving node to re-accept, re-notify, and re-broadcast it network-wide.

---

### Finding Description

**Two independent tracking structures are mismatched — direct analog to the LiquidityManager nonce mismatch:**

`Notifier` maintains two separate structures for deduplication:

- `received_alerts: HashMap<u32, Alert>` — live alerts keyed by `alert_id`
- `cancel_filter: LruCache<u32, ()>` — explicitly cancelled alert IDs [1](#0-0) 

`has_received()` checks both: [2](#0-1) 

`clear_expired_alerts()` removes expired entries from `received_alerts` and `noticed_alerts`, but **never inserts the evicted IDs into `cancel_filter`**: [3](#0-2) 

After expiry and cleanup, `has_received(alert_id)` returns `false` for that ID. The ID is in neither map.

**The P2P receive path has no `notice_until` guard:**

`AlertRelayer::received()` checks only `has_received()` and signature validity before accepting and re-broadcasting: [4](#0-3) 

There is no `notice_until < now` rejection anywhere in this path. Compare with the RPC path, which does enforce it: [5](#0-4) 

**`Notifier::add()` also has no expiry check:** [6](#0-5) 

The expired alert is re-inserted into `received_alerts` and `notify_controller.notify_network_alert()` fires again.

**`clear_expired_alerts()` is triggered by peer connection events**, giving the attacker a reliable trigger: [7](#0-6) 

---

### Impact Explanation

An unprivileged P2P peer can replay any previously-expired, validly-signed alert (e.g., a past "critical bug — stop transacting" message) after its `notice_until` has passed. Every node that accepts the replay:

1. Re-inserts it into `received_alerts`
2. Fires `notify_controller.notify_network_alert()`, triggering all registered subscribers and any configured `network_alert_notify_script`
3. Re-broadcasts it to all connected peers via `quick_filter_broadcast`

The result is a network-wide re-propagation of a stale alert, causing spurious user notifications and script executions across all reachable nodes. The CKB consensus engine is not affected, but the alert system's integrity guarantee — that a signed alert is acted upon exactly once within its validity window — is broken.

---

### Likelihood Explanation

The attacker preconditions are minimal: connect as a standard P2P peer (no privileged keys required) and possess the raw bytes of any previously-broadcast alert (observable from the public P2P network). The trigger is deterministic: connecting to a node causes `clear_expired_alerts()` to run, after which the same peer can immediately send the expired alert. Any node that has ever been online during a past alert broadcast is a valid target.

---

### Recommendation

1. **In `AlertRelayer::received()`**: add an explicit `notice_until` check immediately after parsing, mirroring the RPC guard:
   ```rust
   let notice_until: u64 = alert.as_reader().raw().notice_until().into();
   if notice_until < ckb_systemtime::unix_time_as_millis() {
       return; // silently drop expired alerts from peers
   }
   ```

2. **In `Notifier::clear_expired_alerts()`**: insert evicted alert IDs into `cancel_filter` before removing them from `received_alerts`, so `has_received()` remains `true` for expired IDs:
   ```rust
   self.received_alerts.retain(|id, alert| {
       if alert.raw().notice_until() <= now { self.cancel_filter.put(*id, ()); false }
       else { true }
   });
   ```

3. **In `Notifier::add()`**: add a `notice_until` guard as a defense-in-depth measure.

---

### Proof of Concept

1. Node A broadcasts alert `id=42`, `notice_until=T`, with valid 2-of-4 signatures. Attacker captures the raw P2P bytes.
2. Time advances past `T`. Node A's next `connected()` event calls `clear_expired_alerts()`, removing `id=42` from `received_alerts`. `cancel_filter` does not contain `42`.
3. Attacker connects to Node A as a P2P peer (this itself triggers `clear_expired_alerts()` if not already done).
4. Attacker sends the captured bytes for alert `id=42` via the Alert protocol.
5. `AlertRelayer::received()`: `has_received(42)` → `false`; `verify_signatures()` → `Ok(())`; `notifier.add(&alert)` → re-inserts `id=42`, fires `notify_network_alert`.
6. `quick_filter_broadcast` re-sends the alert to all of Node A's peers, who repeat steps 4–6.
7. The expired alert propagates network-wide; all nodes display the stale alert message and execute any configured `network_alert_notify_script`.

### Citations

**File:** util/network-alert/src/notifier.rs (L12-21)
```rust
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

**File:** util/network-alert/src/notifier.rs (L93-116)
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
