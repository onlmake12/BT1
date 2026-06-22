### Title
Expired Network Alert Replay via P2P — Missing `notice_until` Validation in P2P Handler Allows Indefinite Replay of Expired Signed Alerts - (File: `util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network alert system allows signed alert messages to be replayed over P2P after they expire. The `clear_expired_alerts` function removes expired alerts from the deduplication cache (`received_alerts`), but the P2P `received` handler never validates the `notice_until` field. Any peer that stored the original alert bytes can re-inject the expired, still-cryptographically-valid alert after expiry, causing it to be re-accepted, re-broadcast network-wide, and re-displayed to all node operators.

---

### Finding Description

The `Notifier` struct maintains three state collections:

- `received_alerts: HashMap<u32, Alert>` — deduplication map keyed by alert ID
- `cancel_filter: LruCache<u32, ()>` — IDs of explicitly cancelled alerts
- `noticed_alerts: Vec<Alert>` — alerts shown to users

`clear_expired_alerts` removes entries from `received_alerts` and `noticed_alerts` based on `notice_until`, but does **not** add the expired IDs to `cancel_filter`: [1](#0-0) 

After this runs, `has_received(expired_id)` returns `false` because the ID is absent from both `received_alerts` and `cancel_filter`: [2](#0-1) 

The P2P `received` handler in `AlertRelayer` uses only `has_received` as its deduplication gate, and performs **no** `notice_until` check: [3](#0-2) 

By contrast, the RPC `send_alert` entry point does enforce `notice_until`: [4](#0-3) [5](#0-4) 

`clear_expired_alerts` is called on every new peer connection and on every `get_blockchain_info` RPC call, so the deduplication window opens regularly: [6](#0-5) 

Once the window opens, a peer that stored the original alert bytes (which are broadcast publicly) can re-send them. The handler will pass the `has_received` check (returns `false`), pass `verify_signatures` (ECDSA signatures never expire), re-insert the alert into `received_alerts` and `noticed_alerts`, and re-broadcast it to all connected peers. [7](#0-6) 

---

### Impact Explanation

Any P2P peer that received and stored the original alert bytes can replay an expired alert after `clear_expired_alerts` runs. The replayed alert is:

1. Re-accepted as a new alert by the receiving node
2. Re-broadcast to all connected peers (network-wide propagation)
3. Re-displayed to node operators via `get_blockchain_info`

This allows stale or resolved critical alerts (e.g., "upgrade your node immediately — critical bug") to be re-injected after the issue has been resolved, causing operator confusion, unnecessary panic, or masking of new legitimate alerts. The `noticed_alerts` list shown to users will contain the replayed expired alert.

**Impact: Medium**

---

### Likelihood Explanation

Alert messages are broadcast publicly over the P2P network. Every node that received the original alert has the full signed bytes. After the alert's `notice_until` timestamp passes and `clear_expired_alerts` is triggered (which happens on every peer connection and every `get_blockchain_info` call), any of those peers can replay the alert. No key compromise is required — only possession of the original broadcast bytes.

**Likelihood: Medium**

---

### Recommendation

Add a `notice_until` expiry check in the P2P `received` handler in `alert_relayer.rs`, mirroring the check already present in the RPC handler:

```rust
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
let now_ms = ckb_systemtime::unix_time_as_millis();
if notice_until < now_ms {
    // silently drop or disconnect peer
    return;
}
```

Additionally, when `clear_expired_alerts` removes an alert from `received_alerts`, it should add the expired ID to `cancel_filter` to permanently block replay:

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
        self.cancel_filter.put(id, ());
    }
    // ... same for noticed_alerts
}
```

---

### Proof of Concept

1. Node A and Node B are connected. Node A sends a valid signed alert (ID=1, `notice_until = T`).
2. Both nodes receive and store the alert. Node B stores the raw alert bytes.
3. Time advances past `T`. Node C connects to Node A, triggering `clear_expired_alerts`.
4. Alert ID=1 is removed from `received_alerts`. `has_received(1)` now returns `false`.
5. Node B re-sends the original alert bytes (ID=1) to Node A via the P2P Alert protocol.
6. Node A's `received` handler: `has_received(1)` → `false` (passes), `verify_signatures` → `Ok` (ECDSA signatures are still valid), alert is re-added to `received_alerts` and `noticed_alerts`, and re-broadcast to all peers.
7. `get_blockchain_info` on Node A now shows the expired alert again. [8](#0-7) [1](#0-0) [9](#0-8)

### Citations

**File:** util/network-alert/src/notifier.rs (L9-14)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
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

**File:** util/network-alert/src/alert_relayer.rs (L87-88)
```rust
        self.clear_expired_alerts();
        for alert in self.notifier.lock().received_alerts() {
```

**File:** util/network-alert/src/alert_relayer.rs (L97-179)
```rust
    #[allow(clippy::needless_collect)]
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let alert: packed::Alert = match packed::AlertReader::from_slice(&data) {
            Ok(alert) => {
                if alert.raw().message().is_utf8()
                    && alert
                        .raw()
                        .min_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                    && alert
                        .raw()
                        .max_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                {
                    alert.to_entity()
                } else {
                    info!(
                        "A malformed message fromP peer {} : not utf-8 string",
                        peer_index
                    );
                    nc.ban_peer(
                        peer_index,
                        BAD_MESSAGE_BAN_TIME,
                        String::from("send us a malformed message: not utf-8 string"),
                    );
                    return;
                }
            }
            Err(err) => {
                info!("A malformed message from peer {}: {:?}", peer_index, err);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
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

**File:** rpc/src/module/alert.rs (L102-110)
```rust
    fn send_alert(&self, alert: Alert) -> Result<()> {
        let alert: packed::Alert = alert.into();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until < now_ms {
            return Err(RPCError::invalid_params(format!(
                "Expected `params[0].notice_until` in the future (> {now_ms}), got {notice_until}",
            )));
        }
```
