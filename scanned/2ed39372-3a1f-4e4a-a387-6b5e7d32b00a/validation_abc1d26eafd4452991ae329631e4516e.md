### Title
Expired Network Alert Replay via P2P — Any Peer Can Re-inject Expired Signed Alerts After `clear_expired_alerts()` Drops Replay Protection (`util/network-alert/src/notifier.rs`)

---

### Summary

The `Notifier::clear_expired_alerts()` function removes expired alerts from `received_alerts` but does not record their IDs in any persistent replay-prevention structure. After expiry, `has_received()` returns `false` for those IDs, allowing any P2P peer to replay a previously valid signed alert. The replayed alert passes both the dedup check and signature verification (signatures remain cryptographically valid over the alert content), gets re-broadcast to all connected peers, and re-triggers the `notify_network_alert` callback — including any configured `network_alert_notify_script`.

---

### Finding Description

**Root cause — `clear_expired_alerts()` drops IDs without recording them:**

`Notifier::clear_expired_alerts()` removes entries from `received_alerts` and `noticed_alerts` based on the `notice_until` timestamp, but never adds the expired IDs to `cancel_filter` or any other seen-set:

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
``` [1](#0-0) 

**`has_received()` only checks `received_alerts` and `cancel_filter`:**

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [2](#0-1) 

Once `clear_expired_alerts()` removes an alert from `received_alerts`, and the ID was never explicitly cancelled (so it is not in `cancel_filter`), `has_received(id)` returns `false`. The ID is now "forgotten."

**P2P `received()` handler accepts the replayed alert:**

The `AlertRelayer::received()` handler checks `has_received()` before signature verification. A replayed expired alert passes both checks:

1. `has_received(alert_id)` → `false` (ID was cleared) → does not return early
2. `verifier.verify_signatures(&alert)` → passes (secp256k1 signatures over the alert hash are still cryptographically valid)
3. Alert is re-broadcast to all connected peers
4. `notifier.add(&alert)` is called, which calls `notify_network_alert` [3](#0-2) 

**`Notifier::add()` re-triggers the notification callback:**

```rust
self.notify_controller.notify_network_alert(alert.clone());
self.noticed_alerts.push(alert.clone());
``` [4](#0-3) 

**`cancel_filter` is bounded at 128 entries (LruCache), not a persistent set:**

Even for explicitly cancelled alerts, the `cancel_filter` can evict old entries after 128 cancellations, creating a secondary replay window for cancelled alerts as well. [5](#0-4) 

**`send_alert` RPC checks `notice_until`, but the P2P handler does not:**

The RPC path rejects expired alerts:
```rust
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [6](#0-5) 

The P2P `received()` handler has no such check, making the P2P path the only viable replay vector. [7](#0-6) 

---

### Impact Explanation

Any P2P peer (zero privilege required) who saved a previously broadcast alert can replay it after expiry to:

1. **Network-wide re-broadcast amplification**: The receiving node re-broadcasts to all connected peers, who each re-broadcast to their peers, causing a network-wide flood of stale alert traffic.
2. **Re-trigger `notify_network_alert` callback**: Any node with `network_alert_notify_script` configured in `ckb.toml` will re-execute that script for each replayed alert, potentially causing repeated false alarms or unintended side effects.
3. **False alert display**: The alert is re-added to `noticed_alerts` and will appear in `get_blockchain_info()` responses until the next `clear_expired_alerts()` call, misleading node operators.

The impact is network-wide disruption of the alert system's integrity — the same signed message produces unauthorized repeated effects, directly analogous to the external report's cross-function signature replay.

---

### Likelihood Explanation

**High.** The attacker preconditions are trivially met:
- Connect as a P2P peer (no authentication required on the CKB P2P network)
- Save any alert bytes observed on the P2P network (alerts are broadcast to all peers)
- Wait for the alert to expire
- Re-send the saved bytes

No key material, no privileged access, no cryptographic attack is needed. The alert bytes are public and the replay window opens automatically after `notice_until` passes.

---

### Recommendation

In `clear_expired_alerts()`, record expired alert IDs in `cancel_filter` before removing them from `received_alerts`:

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
        self.cancel_filter.put(id, ()); // prevent replay
        self.received_alerts.remove(&id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, consider replacing `cancel_filter: LruCache<u32, ()>` with an unbounded `HashSet<u32>` or a set with a much larger capacity, since the current 128-entry LRU bound also allows replay of old cancelled alert IDs after sufficient new cancellations. [8](#0-7) 

---

### Proof of Concept

1. Connect to a CKB mainnet/testnet node as a P2P peer using the Tentacle protocol.
2. Observe and record the raw bytes of any valid alert message relayed on the network (alerts are broadcast to all peers on connection via `connected()` handler).
3. Wait until the alert's `notice_until` timestamp passes.
4. Confirm the alert is cleared: `get_blockchain_info()` returns an empty `alerts` array.
5. Send the saved raw alert bytes to the node via the Alert P2P protocol.
6. Observe: the node accepts the message (passes `has_received()` and `verify_signatures()`), re-broadcasts it to all connected peers, and re-triggers `notify_network_alert` — including executing any configured `network_alert_notify_script`.
7. Each peer that receives the re-broadcast will repeat steps 5–6, causing network-wide propagation of the stale alert. [9](#0-8) [10](#0-9)

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

**File:** util/network-alert/src/alert_relayer.rs (L98-178)
```rust
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
