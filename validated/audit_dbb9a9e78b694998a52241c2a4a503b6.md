### Title
P2P Alert Relay Path Accepts and Re-Broadcasts Expired Alerts Without Checking `notice_until` - (`util/network-alert/src/alert_relayer.rs`)

---

### Summary

The `AlertRelayer::received` handler in `util/network-alert/src/alert_relayer.rs` verifies only the cryptographic signatures of an incoming P2P alert, but never checks whether the alert's `notice_until` expiration timestamp has passed. Any unprivileged P2P peer can replay a previously valid but now-expired alert, causing the receiving node to accept it, re-broadcast it to all connected peers, and display it to the node operator — indefinitely and repeatedly.

---

### Finding Description

The `Alert` type in CKB contains a `notice_until` field (a millisecond UTC timestamp) that defines when the alert expires. [1](#0-0) 

The `notice_until` value is part of `RawAlert`, which is hashed and signed by Nervos Foundation key-holders. The signature therefore commits to the expiration time.

The **RPC entry point** (`rpc/src/module/alert.rs`) correctly enforces this expiration before accepting an alert: [2](#0-1) 

However, the **P2P relay entry point** — `AlertRelayer::received` in `util/network-alert/src/alert_relayer.rs` — performs no such check. After parsing the message, it only calls `verify_signatures`: [3](#0-2) 

`verify_signatures` in `util/network-alert/src/verifier.rs` checks only the cryptographic validity of the signatures; it never reads or compares `notice_until` against the current time: [4](#0-3) 

After passing signature verification, the expired alert is immediately re-broadcast to all connected peers and added to the notifier (which surfaces it to the operator): [5](#0-4) 

`Notifier::add` also performs no expiration check before inserting into `received_alerts` and calling `notify_controller.notify_network_alert`: [6](#0-5) 

Expiration cleanup (`clear_expired_alerts`) is only triggered in the `connected` callback — when a new peer connects — not when an alert is received: [7](#0-6) 

This creates a secondary problem: `clear_expired_alerts` removes the expired alert from `received_alerts`, which causes `has_received` to return `false` for that alert ID again: [8](#0-7) 

This means the same expired alert can be injected repeatedly: each time a new peer connects and triggers cleanup, the deduplication guard is reset, and the next peer that sends the old alert will cause the node to accept, re-broadcast, and re-notify it again.

---

### Impact Explanation

1. **Operator deception**: Node operators are shown stale alerts (e.g., "upgrade your node" warnings that no longer apply), potentially causing unnecessary or incorrect operational responses.
2. **Network-wide re-broadcast**: Every node that accepts the expired alert immediately fans it out to all connected peers, propagating the stale message across the entire P2P network.
3. **Repeated injection**: Because `clear_expired_alerts` resets the deduplication state, a malicious peer can re-inject the same expired alert after each new peer connection, creating a persistent spam/confusion loop with no rate limit.

---

### Likelihood Explanation

Any unprivileged P2P peer that has observed or stored a previously valid alert (which are broadcast publicly across the network) can replay it after expiration. No key material, privileged access, or majority hashpower is required. Old alerts are observable by all network participants during their validity window, making them trivially available for replay.

---

### Recommendation

In `AlertRelayer::received`, add an expiration check immediately after signature verification, mirroring the check already present in the RPC path:

```rust
// After verify_signatures succeeds:
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    debug!(
        "Received an expired alert {} from peer {}, ignoring",
        alert_id, peer_index
    );
    return;
}
```

Additionally, consider adding an expiration check inside `Notifier::add` as a defense-in-depth measure, so that no expired alert can ever reach `notify_controller.notify_network_alert` regardless of the call site.

---

### Proof of Concept

1. Observe a valid alert broadcast on the network during its validity window; record its raw bytes (including the valid multi-signature).
2. Wait until `notice_until` has passed (the alert expires).
3. Connect to any CKB node as a P2P peer using the Alert protocol (`SupportProtocols::Alert`).
4. Send the raw bytes of the expired alert.
5. Observe that the node:
   - Does **not** reject the message.
   - Re-broadcasts it to all its connected peers.
   - Surfaces it to the operator via `notify_network_alert`.
6. Wait for a new peer to connect to the target node (triggering `clear_expired_alerts` and resetting `has_received`).
7. Repeat step 4 — the node accepts and re-broadcasts the same expired alert again.

### Citations

**File:** util/gen-types/schemas/extensions.mol (L445-453)
```text
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
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

**File:** util/network-alert/src/alert_relayer.rs (L150-162)
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
```

**File:** util/network-alert/src/alert_relayer.rs (L163-178)
```rust
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

**File:** util/network-alert/src/verifier.rs (L33-64)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
        let signatures: Vec<Signature> = alert
            .signatures()
            .into_iter()
            .filter_map(
                |sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
                    Ok(sig) => {
                        if sig.is_valid() {
                            Some(sig)
                        } else {
                            debug!("invalid signature: {:?}", sig);
                            None
                        }
                    }
                    Err(err) => {
                        debug!("signature error: {}", err);
                        None
                    }
                },
            )
            .collect();
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
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
