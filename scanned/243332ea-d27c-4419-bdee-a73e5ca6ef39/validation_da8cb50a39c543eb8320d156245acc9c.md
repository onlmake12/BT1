### Title
Cancelled Network Alerts Can Be Replayed After Node Restart Due to Ephemeral-Only `cancel_filter` — (`File: util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system tracks cancelled alert IDs exclusively in an in-memory `LruCache` (`cancel_filter`, capacity 128). This state is never persisted. After any node restart, all cancellation records are lost, and any P2P peer holding a previously-broadcast alert can replay it. The replayed alert passes signature verification (signatures are over static content, not a nonce), is accepted by the node, displayed to the operator, and re-broadcast to the network. Additionally, even without a restart, the bounded LRU can be evicted after 128 cancellations, producing the same replay window.

---

### Finding Description

`Notifier` in `util/network-alert/src/notifier.rs` maintains three in-memory data structures:

- `cancel_filter: LruCache<u32, ()>` — capacity `CANCEL_FILTER_SIZE = 128` — the sole record of which alert IDs have been cancelled.
- `received_alerts: HashMap<u32, Alert>` — alerts currently held.
- `noticed_alerts: Vec<Alert>` — alerts shown to the operator. [1](#0-0) 

When `cancel(cancel_id)` is called, the cancelled ID is inserted into `cancel_filter` and removed from `received_alerts`: [2](#0-1) 

The only replay guard is `has_received()`, which checks both maps: [3](#0-2) 

**Failure mode 1 — node restart**: All three structures are initialized empty on startup. After restart, `has_received(X)` returns `false` for every alert ID, including previously-cancelled ones. Any peer that stored the raw bytes of a cancelled alert can send it; the node will pass it through `verify_signatures`, accept it, re-broadcast it, and display it.

**Failure mode 2 — LRU eviction**: `cancel_filter` holds at most 128 entries. Once 128 additional cancellations occur, the oldest cancelled ID is silently evicted. `has_received()` then returns `false` for that ID, re-opening the replay window without any restart.

The `clear_expired_alerts` helper, called on every peer connection, purges `received_alerts` and `noticed_alerts` by `notice_until` timestamp, but **never touches `cancel_filter`**: [4](#0-3) 

The P2P entry path is `AlertRelayer::received`, which checks `has_received` before signature verification: [5](#0-4) 

After restart (or LRF eviction), `has_received` returns `false`, so the check is bypassed and the replayed alert proceeds to `verify_signatures` — which passes because alert signatures are over static content (the `RawAlert` hash), not over any per-session nonce or sequence number. [6](#0-5) 

---

### Impact Explanation

Any P2P peer (no privilege required) who retained the raw bytes of a previously-cancelled alert can replay it to any restarted CKB node. The node will:

1. Accept the alert (signature valid, ID not in empty `cancel_filter`).
2. Display it to the node operator via `notify_controller.notify_network_alert`.
3. Re-broadcast it to all connected peers, propagating the stale/cancelled alert network-wide.

Concrete consequences:
- A cancelled "critical bug — upgrade immediately" alert can be re-injected after a routine software upgrade, causing operator panic and incorrect upgrade actions.
- A high-priority replayed alert can displace a current active alert in `noticed_alerts` (sorted by priority), suppressing awareness of a real ongoing emergency.
- The integrity of the alert system — the only out-of-band emergency communication channel for CKB — is permanently undermined for any node that has ever restarted.

---

### Likelihood Explanation

**High** for the restart vector. CKB nodes restart routinely for software upgrades, configuration changes, and hardware maintenance. Every restart resets `cancel_filter` to empty. Any peer on the network that stored old alert bytes (trivial to do) can immediately replay cancelled alerts to the restarted node. No cryptographic capability is required — the attacker only needs to have observed the P2P network at any prior point.

**Medium** for the LRU eviction vector. It requires 128 subsequent cancellations to evict a specific entry, which depends on future alert issuance volume. However, the LRU capacity of 128 is a hard ceiling with no overflow protection or warning.

---

### Recommendation

1. **Persist `cancel_filter` to disk** (e.g., in the existing RocksDB store) so cancellation records survive restarts. The set of cancelled alert IDs is small and append-only.
2. **Replace the bounded `LruCache` with an unbounded `HashSet`** for `cancel_filter`, or at minimum increase the capacity significantly and add a log warning when the LRU is near capacity.
3. **Include a monotonic sequence number or nonce in the alert signing payload** so that a replayed alert with an old sequence number can be rejected even if the ID is not in `cancel_filter`.

---

### Proof of Concept

**Restart replay (no privilege required):**

```
1. Attacker connects to the CKB P2P network and records all broadcast alert messages (raw bytes).
2. Developers issue alert id=42 (e.g., "critical bug in v0.105").
3. Developers cancel alert 42 by issuing alert id=43 (cancel=42).
4. Node operator restarts the node (e.g., for a software upgrade).
5. Attacker connects to the restarted node and sends the raw bytes of alert id=42.
6. Node calls has_received(42) → false (cancel_filter is empty after restart).
7. Node calls verify_signatures(alert_42) → Ok (signatures are still valid).
8. Node calls notifier.add(&alert_42):
   - Inserts id=42 into received_alerts.
   - Pushes alert_42 into noticed_alerts.
   - Calls notify_controller.notify_network_alert(alert_42).
9. Node re-broadcasts alert_42 to all connected peers.
10. All peers that have also restarted accept and re-broadcast the cancelled alert.
```

Relevant code path: [7](#0-6) [8](#0-7)

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

**File:** util/network-alert/src/notifier.rs (L37-44)
```rust
        Notifier {
            cancel_filter: LruCache::new(CANCEL_FILTER_SIZE),
            received_alerts: Default::default(),
            noticed_alerts: Vec::new(),
            client_version: parsed_client_version,
            notify_controller,
        }
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

**File:** util/network-alert/src/notifier.rs (L124-132)
```rust
    /// Cancel alert id
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

**File:** util/network-alert/src/alert_relayer.rs (L144-179)
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
    }
```
