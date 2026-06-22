### Title
Expired Network Alerts Accepted and Re-broadcast via P2P Without Deadline Check - (`util/network-alert/src/alert_relayer.rs`)

### Summary

The P2P `received()` handler in `AlertRelayer` does not check the `notice_until` expiry field before accepting, storing, and re-broadcasting a network alert. Any unprivileged P2P peer can replay a previously valid but now-expired signed alert, causing every receiving node to accept it, add it to its active alert list, trigger user notifications, and re-broadcast it to all connected peers.

### Finding Description

The RPC entry point `send_alert` in `rpc/src/module/alert.rs` correctly rejects expired alerts:

```rust
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [1](#0-0) 

However, the P2P `received()` handler in `util/network-alert/src/alert_relayer.rs` performs no equivalent check. After parsing the alert and verifying signatures, it immediately re-broadcasts and calls `notifier.add()`:

```rust
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
// mark sender as known
self.mark_as_known(peer_index, alert_id);
// broadcast message
...
// add to received alerts
self.notifier.lock().add(&alert);
``` [2](#0-1) 

`Notifier::add()` also performs no expiry check — it only checks version range and deduplication: [3](#0-2) 

`clear_expired_alerts` is only invoked inside `connected()` (when a new peer connects), never inside `received()`: [4](#0-3) 

Because `notice_until` is part of the signed `RawAlert` payload (it is included in `calc_alert_hash()`), an attacker cannot forge a new alert. However, all previously broadcast alerts are public. An attacker can replay any old, expired, legitimately-signed alert to any node that has not yet seen it (e.g., a newly synced node, or a node that restarted and cleared its state).

### Impact Explanation

A malicious P2P peer replays an expired but validly-signed alert to a target node. The node:
1. Passes signature verification (signatures are still cryptographically valid).
2. Stores the alert in `received_alerts` and `noticed_alerts`.
3. Calls `notify_controller.notify_network_alert()`, triggering user-visible notifications with stale/misleading content (e.g., "upgrade immediately" for a bug that was patched months ago).
4. Re-broadcasts the expired alert to all its connected peers, propagating the stale alert across the network.

This causes misleading user-facing warnings and unnecessary network-wide re-propagation of stale signed payloads — a direct analog to the Forwarder issue where signed requests execute long after the signer's intent window has closed.

### Likelihood Explanation

Any unprivileged P2P peer can trigger this. Previously broadcast alerts are public record. A node that restarts, or a newly connected node that has not yet seen a historical alert, will have an empty `received_alerts` map and will accept the replayed expired alert. No special keys, no privileged access, and no majority hashpower are required.

### Recommendation

Add an expiry check in `AlertRelayer::received()` immediately after parsing, mirroring the check already present in `send_alert`:

```rust
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
let now_ms = ckb_systemtime::unix_time_as_millis();
if notice_until < now_ms {
    debug!("Ignoring expired alert {} from peer {}", alert_id, peer_index);
    return;
}
```

Additionally, add the same check inside `Notifier::add()` as a defense-in-depth guard, so that no expired alert can enter `received_alerts` or `noticed_alerts` regardless of the call site.

### Proof of Concept

1. Obtain any previously broadcast, now-expired CKB network alert (e.g., alert `20230001` with `notice_until = 1681574400000`, which expired in April 2023).
2. Connect to a CKB node as a P2P peer using the Alert protocol.
3. Send the raw bytes of the expired alert.
4. Observe that the node: (a) does not reject it, (b) adds it to its active alert list, (c) triggers `notify_network_alert`, and (d) re-broadcasts it to all its connected peers.

The `test_alert_20230001` test in `util/network-alert/src/tests/generate_alert_signature.rs` confirms that the signatures for this expired alert remain cryptographically valid and pass `verify_signatures` today: [5](#0-4)

### Citations

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

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L58-93)
```rust
#[test]
fn test_alert_20230001() {
    let config = NetworkAlertConfig::default();
    let verifier = Verifier::new(config);
    let raw_alert = packed::RawAlert::new_builder()
        // 3 months later
        .notice_until(1681574400000u64)
        .id(20230001u32)
        .cancel(0u32)
        .priority(20u32)
        .message("CKB v0.105.* have bugs. Please upgrade to the latest version.")
        .min_version(Some("0.105.0-pre"))
        .max_version(Some("0.105.1"))
        .build();

    let signatures = [
        "8dca283684ff3cd024bd6a67efb24617e90e31dc69ac809ac6ac5e243a57b7aa6711228dfbd8a5cc89a68d3065b685e5c56c70740e8d3487fd538dc914d0c97c00",
        "4554b37824e17ea02432507e372c869301a415bf718e0a5a33b6df75cd32fbab7cf8176ca8b079c28266ce1f33c3f61fbff19e27be2a85f5a14faa2b1b474e0a01"
    ].iter().map(|hex| {
        let mut buf = vec![0u8; hex.len() / 2];
        hex_decode(hex.as_bytes(), &mut buf).expect("valid hex");
        buf.into()
    }).fold(packed::BytesVec::new_builder(), |builder, item: packed::Bytes| {
        builder.push(item)
    }).build();
    let alert = packed::Alert::new_builder()
        .raw(raw_alert)
        .signatures(signatures)
        .build();
    let alert_json = Alert::from(alert.clone());
    println!(
        "Alert:\n{}",
        serde_json::to_string_pretty(&alert_json).unwrap()
    );
    assert!(verifier.verify_signatures(&alert).is_ok());
}
```
