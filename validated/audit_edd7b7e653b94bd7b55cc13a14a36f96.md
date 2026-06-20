The code confirms a real behavioral divergence. Here is the full analysis:

---

**RPC path** (`rpc/src/module/alert.rs`, `AlertRpcImpl::send_alert`):

Lines 104–110 explicitly reject expired alerts: [1](#0-0) 

**P2P path** (`util/network-alert/src/alert_relayer.rs`, `AlertRelayer::received`):

The handler parses the alert, deduplicates by ID, verifies signatures, then immediately broadcasts and stores — with **no expiry check** anywhere in the flow: [2](#0-1) 

`Notifier::add()` also performs no expiry check before inserting into `received_alerts`, pushing to `noticed_alerts`, and firing `notify_network_alert`: [3](#0-2) 

`clear_expired_alerts` is only called lazily — in `connected()` when a new peer connects, and in `get_blockchain_info` RPC — never inside `received()`: [4](#0-3) 

---

### Title
Expired Alert Accepted and Re-broadcast via P2P Due to Missing Expiry Check in `received()` — (`util/network-alert/src/alert_relayer.rs`)

### Summary
The P2P `received()` handler in `AlertRelayer` accepts and re-broadcasts alerts without checking whether `notice_until` is in the past, while the RPC `send_alert` handler explicitly rejects such alerts. An attacker holding any previously valid (now-expired) signed alert can replay it over P2P, causing every receiving node to store it and flood it to all connected peers.

### Finding Description
`AlertRpcImpl::send_alert` guards at lines 104–110 of `rpc/src/module/alert.rs`:
```rust
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
```
No equivalent guard exists in `AlertRelayer::received()` (`util/network-alert/src/alert_relayer.rs` lines 98–179). After signature verification passes, the handler unconditionally:
1. Broadcasts the raw bytes to all connected peers (lines 166–176).
2. Calls `self.notifier.lock().add(&alert)` (line 178), which inserts the alert into `received_alerts` and `noticed_alerts` and fires `notify_network_alert` (notifier.rs lines 104, 115–116).

`clear_expired_alerts` is only invoked lazily on peer connection events (line 87) or via `get_blockchain_info` RPC, so the expired alert persists until one of those events occurs.

### Impact Explanation
- Every node that receives the replayed alert re-broadcasts it to all its peers, creating network-wide amplification.
- The expired alert is stored in `received_alerts` and `noticed_alerts` and surfaced via `notify_network_alert`, potentially displaying a stale critical warning to node operators.
- The `has_received` deduplication (line 147) prevents a second replay of the same alert ID, but the first replay propagates fully before any node can suppress it.

### Likelihood Explanation
The attacker requires only a previously broadcast, validly-signed alert (2-of-4 Nervos Foundation keys). Any such alert is public — it was broadcast to the entire network when originally sent. No key material or privileged access is needed; a passive observer who captured the original P2P bytes can replay them at any time after expiry.

### Recommendation
Add an expiry check at the top of `AlertRelayer::received()`, mirroring the RPC guard:
```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // optionally ban peer or just silently drop
    return;
}
```
Optionally, also add the same check inside `Notifier::add()` as a defense-in-depth measure.

### Proof of Concept
1. Capture any previously broadcast, now-expired CKB network alert (valid 2-of-4 signatures, `notice_until` in the past).
2. Open a raw P2P connection to a target node on the Alert protocol.
3. Send the expired alert bytes.
4. **Expected (RPC):** `send_alert` with the same payload returns `InvalidParams` — rejected.
5. **Observed (P2P):** The node accepts the alert, stores it in `received_alerts`/`noticed_alerts`, fires `notify_network_alert`, and re-broadcasts it to all connected peers.
6. Confirm propagation by observing the alert appear in connected peers' `noticed_alerts` before any `clear_expired_alerts` call occurs.

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

**File:** util/network-alert/src/alert_relayer.rs (L58-61)
```rust
    fn clear_expired_alerts(&mut self) {
        let now = ckb_systemtime::unix_time_as_millis();
        self.notifier.lock().clear_expired_alerts(now);
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L147-178)
```rust
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
