### Title
Expired Alert Accepted and Re-broadcast via P2P Without `notice_until` Validation — (`util/network-alert/src/alert_relayer.rs`)

### Summary

The `AlertRelayer::received` P2P handler accepts and re-broadcasts network alerts without checking whether the alert's `notice_until` deadline has already passed. The `notice_until` field is defined, stored, and checked in the local RPC path, but is entirely absent from the P2P ingestion path. Any unprivileged peer can replay a previously valid (but now expired) signed alert to cause every receiving node to store it, fire a `notify_network_alert` notification, and re-broadcast it to all of its own peers.

### Finding Description

`Alert` carries a `notice_until` timestamp (milliseconds since UNIX epoch) that marks when the alert expires. [1](#0-0) 

The local `send_alert` RPC correctly rejects alerts whose deadline has already passed:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [2](#0-1) 

The P2P handler `AlertRelayer::received` performs no equivalent check. After parsing the message and verifying signatures it immediately marks the alert as known, broadcasts it to all connected peers, and calls `notifier.add()`:

```rust
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
// mark sender as known
self.mark_as_known(peer_index, alert_id);
// broadcast message
...nc.quick_filter_broadcast(..., data)...
// add to received alerts
self.notifier.lock().add(&alert);
``` [3](#0-2) 

`Notifier::add` also performs no expiry check; it stores the alert in `received_alerts` and fires `notify_controller.notify_network_alert`: [4](#0-3) 

`clear_expired_alerts` is only called on the `connected` event (new peer handshake), never inside `received`: [5](#0-4) [6](#0-5) 

Furthermore, `clear_expired_alerts` removes expired entries from `received_alerts` but does **not** add them to `cancel_filter`. This means `has_received` returns `false` for a previously-seen-then-expired alert, so the deduplication guard at line 147 is bypassed and the same expired alert can be re-injected repeatedly after each cleanup cycle: [7](#0-6) [8](#0-7) 

### Impact Explanation

An attacker who possesses any previously broadcast, validly-signed alert (obtainable from historical network traffic or public sources) can replay it after its `notice_until` has passed. Every receiving node will:

1. Pass the signature check (signatures remain cryptographically valid after expiry).
2. Store the expired alert in its `received_alerts` map.
3. Fire `notify_network_alert`, surfacing a stale/false alert to node operators.
4. Re-broadcast the alert to all of its own connected peers, causing network-wide amplification.

After `clear_expired_alerts` runs on the next `connected` event, the alert is evicted without being tombstoned, so the same peer (or any other) can re-inject it again, creating a persistent spam loop.

### Likelihood Explanation

The entry path requires only an unprivileged P2P connection and possession of any past alert with valid signatures. Historical alerts are publicly observable on-chain or via network sniffing. No special privilege, key material, or majority hash power is required. The attack is trivially repeatable.

### Recommendation

Add an expiry check at the top of `AlertRelayer::received`, mirroring the guard already present in the RPC handler:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // silently drop or optionally ban the peer
    return;
}
```

This check should be placed immediately after successful parsing and before signature verification, so that expired replays are discarded without consuming verification resources. Additionally, `clear_expired_alerts` should tombstone evicted IDs into `cancel_filter` (or a dedicated expiry-filter) so that `has_received` correctly deduplicates re-injected expired alerts.

### Proof of Concept

1. Observe any past CKB mainnet/testnet alert with valid signatures (e.g., alert `20230001` whose `notice_until` is `1681574400000`, April 2023).
2. Connect to a live CKB node as a P2P peer on the Alert protocol.
3. Send the raw alert bytes after `notice_until` has passed.
4. Observe: the node accepts the message, fires `notify_network_alert`, and re-broadcasts the expired alert to all its peers — identical to the LPDA `buy()` accepting purchases after `endTime`.

### Citations

**File:** util/jsonrpc-types/src/alert.rs (L55-56)
```rust
    /// The alert is expired after this timestamp.
    pub notice_until: Timestamp,
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

**File:** util/network-alert/src/alert_relayer.rs (L58-61)
```rust
    fn clear_expired_alerts(&mut self) {
        let now = ckb_systemtime::unix_time_as_millis();
        self.notifier.lock().clear_expired_alerts(now);
    }
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

**File:** util/network-alert/src/alert_relayer.rs (L150-178)
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

**File:** util/network-alert/src/notifier.rs (L146-149)
```rust
    /// Whether id received
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
    }
```
