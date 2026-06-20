### Title
Missing Expiration Validation for P2P-Relayed Alerts Allows Replay of Stale Alerts - (File: `util/network-alert/src/alert_relayer.rs`)

### Summary
The `AlertRelayer::received()` handler processes incoming P2P alert messages without checking whether the alert's `notice_until` timestamp has already passed. Any peer can replay a previously-broadcast, validly-signed but now-expired alert. The node accepts it, stores it, triggers the `notify_network_alert` callback (which can execute a configured shell script), and re-broadcasts it to all connected peers. The RPC `send_alert` entry point correctly rejects expired alerts, but the P2P path has no equivalent staleness guard.

### Finding Description
The `AlertRelayer::received()` function in `util/network-alert/src/alert_relayer.rs` performs the following checks on an incoming P2P alert:

1. UTF-8 validity of string fields
2. Duplicate detection via `notifier.has_received(alert_id)`
3. Signature verification via `verifier.verify_signatures(&alert)`

It does **not** check whether `alert.raw().notice_until()` is still in the future. [1](#0-0) 

After passing those checks, the handler immediately broadcasts the raw bytes to all connected peers and calls `notifier.add(&alert)`: [2](#0-1) 

`Notifier::add()` also contains no expiration check; it inserts the alert into `received_alerts`, conditionally into `noticed_alerts`, and fires `notify_controller.notify_network_alert()`: [3](#0-2) 

`clear_expired_alerts()` is only invoked from the `connected()` callback (when a new peer connects), never from `received()`: [4](#0-3) 

By contrast, the RPC `send_alert` entry point explicitly rejects any alert whose `notice_until` is in the past: [5](#0-4) 

The `notice_until` field is the sole expiration mechanism for alerts: [6](#0-5) 

### Impact Explanation
An attacker who possesses a previously-broadcast, validly-signed alert (one that circulated on the network while it was active) can replay it after its `notice_until` has passed. The receiving node will:

1. Store the expired alert in `received_alerts` (persisting stale data).
2. Push it into `noticed_alerts` and call `notify_controller.notify_network_alert()`, which dispatches the alert to all registered subscribers and, if configured, **executes the operator's `network_alert_notify_script`** with the stale message as an argument.
3. Re-broadcast the expired alert bytes to every connected peer, propagating the stale data across the network.

The result is that node operators see false/stale emergency alerts, and configured notification scripts are executed with outdated content — a direct analog to consuming a stale Chainlink price feed without checking `updatedAt`. [7](#0-6) 

### Likelihood Explanation
Every alert ever broadcast on the CKB mainnet is permanently available to any peer that received it. No special capability is required beyond a standard P2P connection; the attacker simply re-sends the raw bytes of a past alert. The valid 2-of-4 developer signatures are already embedded in the message, so signature verification passes. The only prerequisite is possession of a historical alert message, which any node that was online during a past alert period already has. [8](#0-7) 

### Recommendation
Add an expiration check at the top of `AlertRelayer::received()`, mirroring the guard already present in `send_alert`:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // silently drop or optionally warn; do not ban (peer may have clock skew)
    return;
}
```

This should be placed immediately after parsing and before the `has_received` check, so that expired alerts are discarded before they touch any state or trigger any broadcast. [9](#0-8) 

### Proof of Concept
1. Obtain any previously-broadcast CKB mainnet alert (raw P2P bytes, including valid 2-of-4 developer signatures) whose `notice_until` timestamp is in the past.
2. Connect to a target CKB full node as a standard P2P peer on the Alert protocol (`SupportProtocols::Alert`).
3. Send the raw alert bytes directly to the peer.
4. The node's `AlertRelayer::received()` handler parses the message, finds it has not been seen before (`has_received` returns `false`), passes signature verification, broadcasts it to all of the node's other connected peers, and calls `notifier.add()`.
5. `notifier.add()` fires `notify_controller.notify_network_alert()`, which — if the operator has configured `network_alert_notify_script` — executes that script with the stale alert message as its argument.
6. The expired alert now appears in `get_blockchain_info().alerts` on the target node and all peers it relayed to. [10](#0-9)

### Citations

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

**File:** util/network-alert/src/alert_relayer.rs (L98-179)
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
