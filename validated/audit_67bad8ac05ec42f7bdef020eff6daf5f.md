The code has been verified. All claims in the report check out against the actual source.

**Verification summary:**

1. `clear_expired_alerts` (lines 135–144, `notifier.rs`) removes IDs from `received_alerts` and `noticed_alerts` with no `cancel_filter.put()` call. [1](#0-0) 

2. `has_received` (lines 147–149) gates on both maps; after expiry cleanup, both return false for the cleared ID. [2](#0-1) 

3. `cancel` (lines 125–132) correctly calls `cancel_filter.put` before removing — the asymmetry with `clear_expired_alerts` is real. [3](#0-2) 

4. `connected` calls `clear_expired_alerts` on every inbound peer connection. [4](#0-3) 

5. `received` checks `has_received` (now false), then `verify_signatures` (no expiry check), then broadcasts, then calls `notifier.add`. [5](#0-4) 

6. `verify_signatures` only verifies the cryptographic hash — no `notice_until` check. [6](#0-5) 

7. `notifier.add` also calls `has_received` (false), re-inserts into `received_alerts`, and fires `notify_network_alert` because `noticed_alerts` was also cleared. [7](#0-6) 

All exploit steps are confirmed by the actual code. The impact (network-wide amplified re-propagation at negligible attacker cost) matches the allowed High impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

---

Audit Report

## Title
Expired Alert ID Not Recorded in `cancel_filter` Enables Replay Re-Broadcast — (`util/network-alert/src/notifier.rs`)

## Summary
`clear_expired_alerts` removes expired alert IDs from `received_alerts` and `noticed_alerts` but never inserts them into `cancel_filter`. After expiry, `has_received` returns `false` for those IDs, so any peer holding a copy of a legitimately-signed expired alert can replay it. The victim node re-accepts it, re-broadcasts it to all connected peers, and re-fires `notify_network_alert`, enabling indefinitely repeatable network-wide amplified re-propagation at negligible attacker cost.

## Finding Description
`Notifier` uses two deduplication structures: `received_alerts: HashMap<u32, Alert>` for live alerts and `cancel_filter: LruCache<u32, ()>` for permanently suppressed IDs. `has_received` gates every incoming alert by checking both:

```rust
// notifier.rs L147-149
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```

`clear_expired_alerts` (L135–144) removes expired entries from both `received_alerts` and `noticed_alerts` without touching `cancel_filter`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| { ... });
}
```

By contrast, `cancel` (L125–132) correctly calls `self.cancel_filter.put(cancel_id, ())` before removing — `clear_expired_alerts` is the only removal path that skips this step.

`clear_expired_alerts` is triggered on **every inbound peer connection** via `connected` (L87 of `alert_relayer.rs`). After cleanup, the `received` handler (L147–178 of `alert_relayer.rs`) checks `has_received` (now `false`), then calls `verify_signatures`, which only verifies the cryptographic hash of the alert content with no expiry check. Since the original signatures remain valid over the original content, the replayed alert passes. The handler then broadcasts to all connected peers and calls `notifier.add`, which re-inserts the alert into `received_alerts` and `noticed_alerts` (both were cleared) and fires `notify_network_alert` again.

## Impact Explanation
**High — network congestion with few costs.** The attacker triggers `clear_expired_alerts` by connecting to a victim (cost: one TCP connection), then sends stored alert bytes. The victim re-broadcasts to all its connected peers; each of those peers, if their own `clear_expired_alerts` has also fired, will re-accept and re-broadcast further. The amplification factor equals the average peer fanout. The attacker can repeat this cycle indefinitely by reconnecting, with no key material, no hashpower, and no social engineering required. This matches the allowed High impact: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
The precondition — possessing a copy of a legitimately-signed alert — is trivially met by any node that was online during the original broadcast window. The trigger fires automatically on every inbound peer connection. No special privilege is required. The attack is repeatable without limit and requires only a standard P2P connection.

## Recommendation
In `clear_expired_alerts`, record each removed ID into `cancel_filter` before dropping it, mirroring the `cancel` path:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until <= now {
            self.cancel_filter.put(*id, ());
            false
        } else {
            true
        }
    });
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

This ensures expired IDs are permanently suppressed by `has_received`, consistent with how explicitly cancelled alerts are handled.

## Proof of Concept
1. Run two CKB nodes (victim, attacker). Nervos Foundation broadcasts alert `id=42`, `notice_until=T`. Both nodes receive it; attacker stores the raw signed bytes.
2. Advance system time past `T`.
3. Attacker disconnects and reconnects to victim. The `connected` handler calls `clear_expired_alerts`, removing `id=42` from `received_alerts` and `noticed_alerts`; `cancel_filter` is unchanged.
4. Attacker sends the stored alert bytes over the alert P2P protocol.
5. Victim calls `has_received(42)` → `false` (absent from both maps).
6. `verify_signatures` passes (signatures are still cryptographically valid over the original content).
7. Victim broadcasts to all connected peers and calls `notifier.add`, re-inserting `id=42` and firing `notify_network_alert`.
8. Repeat from step 3 to sustain continuous re-propagation across the network.

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

**File:** util/network-alert/src/notifier.rs (L125-132)
```rust
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

**File:** util/network-alert/src/alert_relayer.rs (L147-179)
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
    }
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
