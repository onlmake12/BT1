### Title
Expired Alert Replay via P2P — Deduplication State Cleared on Expiry Without Permanent Cancel Record (`util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system deduplicates received alerts by alert ID using two structures: `received_alerts` (a `HashMap`) and `cancel_filter` (an LRU cache). When an alert expires, `clear_expired_alerts()` removes it from `received_alerts` but does **not** add its ID to `cancel_filter`. After this cleanup, `has_received(alert_id)` returns `false`, and any P2P peer can replay the original signed alert — which still carries valid Nervos Foundation signatures — causing every node to re-accept, re-display, and re-broadcast the stale message.

---

### Finding Description

**Root cause — `notifier.rs::clear_expired_alerts()` does not tombstone expired IDs** [1](#0-0) 

`clear_expired_alerts` retains only alerts whose `notice_until > now`. Expired entries are silently dropped from `received_alerts` and `noticed_alerts`. Their IDs are never written to `cancel_filter`.

**Deduplication gate — `has_received` checks only the two structures** [2](#0-1) 

Once an expired alert's ID is gone from `received_alerts` and was never placed in `cancel_filter`, `has_received` returns `false`, reopening the gate for that ID.

**`add()` has no `notice_until` guard** [3](#0-2) 

`add()` checks `has_received`, processes the `cancel` field, inserts into `received_alerts`, and pushes to `noticed_alerts` — all without verifying that `notice_until` is in the future.

**P2P relay path has no `notice_until` check** [4](#0-3) 

`received()` only checks UTF-8 validity, `has_received`, and `verify_signatures`. There is no expiry check before the alert is accepted and re-broadcast.

**Contrast: the RPC entry point does check `notice_until`** [5](#0-4) 

`send_alert` rejects alerts whose `notice_until < now_ms`. The P2P relay path has no equivalent guard, creating an asymmetry that the replay exploits.

**Exploit path (step by step)**

1. A legitimate alert (e.g., `id=20230001`) is broadcast by the Nervos Foundation with valid 2-of-4 signatures and `notice_until = T`.
2. An attacker (any P2P peer) observes and stores the raw alert bytes — this is public P2P data.
3. At time `T+1`, a new peer connects to a victim node, triggering `clear_expired_alerts()`. The alert is removed from `received_alerts`; its ID is **not** added to `cancel_filter`.
4. The attacker connects to the victim node and sends the stored alert bytes.
5. `has_received(20230001)` → `false` (not in `received_alerts`, not in `cancel_filter`).
6. `verify_signatures` → passes (the cryptographic signatures are still valid).
7. `add()` inserts the alert into `received_alerts` and `noticed_alerts`; `notify_network_alert` fires.
8. The relay broadcasts the alert to all connected peers, who repeat steps 5–8.

---

### Impact Explanation

Every node that processes the replayed alert will:
- Display the stale alert message to operators via `get_blockchain_info().alerts`.
- Re-broadcast it to all connected peers, causing network-wide propagation of the false alarm.

A past alert warning about a critical bug (e.g., "CKB v0.105.* have bugs. Please upgrade.") could be replayed long after the bug is fixed, causing operators to believe a new critical issue exists, triggering unnecessary emergency upgrades or node shutdowns. The impact is unauthorized state change (alert display) and potential service disruption driven by false information, reachable by any unprivileged P2P peer.

---

### Likelihood Explanation

- **Attacker capability required**: none beyond being a P2P peer and having observed any prior valid alert (public network data).
- **Trigger condition**: the target node must have run `clear_expired_alerts()` since the alert expired, which happens on every new peer connection.
- **Historical alerts**: at least one real alert (`id=20230001`) with known valid signatures exists in the public codebase test fixtures, making the replay immediately constructable.

Likelihood is **medium-high**: the preconditions are trivially met for any node that has been running since a prior alert was active.

---

### Recommendation

In `clear_expired_alerts()`, tombstone each expired alert ID into `cancel_filter` before removing it from `received_alerts`:

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
        self.cancel_filter.put(id, ()); // tombstone before removal
        self.received_alerts.remove(&id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, add a `notice_until` expiry check in `alert_relayer.rs::received()` (mirroring the RPC path) and in `notifier.rs::add()` to reject expired alerts at both entry points.

---

### Proof of Concept

```
1. Capture the raw bytes of alert id=20230001 from the P2P network
   (or reconstruct from the test fixture in
   util/network-alert/src/tests/generate_alert_signature.rs lines 59-92).

2. Wait for the alert's notice_until timestamp to pass.

3. Connect to a victim CKB node as a P2P peer.
   The connection triggers clear_expired_alerts(), evicting id=20230001
   from received_alerts without adding it to cancel_filter.

4. Send the stored alert bytes to the victim node over the Alert protocol.

5. Observe: has_received(20230001) == false → alert accepted,
   verify_signatures passes, alert re-inserted into noticed_alerts,
   broadcast to all peers.

6. Query victim: ckb_get_blockchain_info → alerts array contains
   the stale "CKB v0.105.* have bugs" message.
``` [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** util/network-alert/src/notifier.rs (L135-149)
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

    /// Whether id received
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
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

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L59-92)
```rust
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
```
