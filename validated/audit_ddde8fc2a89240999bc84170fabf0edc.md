All code claims check out against the actual source. Here is the validated report:

Audit Report

## Title
P2P Alert Relay Accepts and Re-Broadcasts Expired Alerts Without Expiration Check - (File: `util/network-alert/src/alert_relayer.rs`)

## Summary
The `AlertRelayer::received` handler verifies only cryptographic signatures on incoming P2P alerts, never checking whether `notice_until` has passed. Any peer can replay a previously valid but now-expired alert, causing the node to accept it, re-broadcast it to all connected peers, and surface it via `notify_network_alert`. Because `clear_expired_alerts` is only triggered on new peer connections, the deduplication guard is periodically reset, enabling unbounded repeated injection of the same expired alert.

## Finding Description
In `util/network-alert/src/alert_relayer.rs`, the `received` handler (lines 98–179) parses the alert, checks `has_received` for deduplication at line 147, then calls `self.verifier.verify_signatures(&alert)` at line 151. No check of `notice_until` against the current time exists anywhere in this path. [1](#0-0) 

`verify_signatures` in `util/network-alert/src/verifier.rs` (lines 33–64) computes the alert hash and validates m-of-n secp256k1 signatures only; it reads no timestamp fields. [2](#0-1) 

By contrast, the RPC entry point `send_alert` in `rpc/src/module/alert.rs` (lines 104–110) explicitly rejects alerts where `notice_until < now_ms` before proceeding to signature verification. [3](#0-2) 

After passing signature verification, the expired alert is immediately re-broadcast to all connected peers (lines 166–176) and passed to `self.notifier.lock().add(&alert)` at line 178. [4](#0-3) 

`Notifier::add` (lines 93–122 of `util/network-alert/src/notifier.rs`) performs no expiration check; it inserts the alert into `received_alerts` and calls `notify_controller.notify_network_alert`. [5](#0-4) 

`clear_expired_alerts` (lines 135–144 of `notifier.rs`) removes expired entries from both `received_alerts` and `noticed_alerts`, but it is only invoked from the `connected` callback at line 87 of `alert_relayer.rs`, not from `received`. [6](#0-5) [7](#0-6) 

After cleanup, `has_received` (lines 147–149 of `notifier.rs`) returns `false` for the now-removed alert ID (unless it is in `cancel_filter`), resetting the deduplication guard and allowing the same expired alert to be injected again on the next receipt. [8](#0-7) 

## Impact Explanation
An attacker can cause every reachable CKB node to re-broadcast an expired alert to all of its connected peers, propagating the stale message across the entire P2P network. Because the deduplication guard is reset each time a new peer connection triggers `clear_expired_alerts`, the attacker can repeat the injection indefinitely with no rate limit beyond the cadence of new peer connections. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
All previously broadcast alerts are observable by every network participant during their validity window. No key material or privileged access is required to record a valid alert. After expiration, any node operator or passive observer who retained the raw alert bytes can connect to a CKB node using `SupportProtocols::Alert` and replay the message. The preconditions are trivially satisfied for any past alert.

## Recommendation
In `AlertRelayer::received` (`util/network-alert/src/alert_relayer.rs`), add an expiration check immediately after the `has_received` guard and before `verify_signatures`, mirroring the check in the RPC path:

```rust
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

As defense-in-depth, add the same check inside `Notifier::add` so that no expired alert can reach `notify_controller.notify_network_alert` regardless of call site. Additionally, consider invoking `clear_expired_alerts` at the start of `received`, not only in `connected`, to keep the deduplication state consistent.

## Proof of Concept
1. Run a CKB node and observe a valid alert broadcast on the network during its validity window; capture the raw serialized bytes (including the valid multi-signature).
2. Wait until `notice_until` has passed.
3. Connect to any CKB node as a P2P peer using `SupportProtocols::Alert`.
4. Send the captured raw bytes.
5. Observe that the node does not reject the message, re-broadcasts it to all connected peers, and surfaces it via `notify_network_alert`.
6. Wait for any new peer to connect to the target node (triggering `clear_expired_alerts` and resetting `has_received` for the expired alert ID).
7. Repeat step 4 — the node accepts and re-broadcasts the same expired alert again.
8. Repeat steps 6–7 to demonstrate unbounded repeated injection.

### Citations

**File:** util/network-alert/src/alert_relayer.rs (L87-87)
```rust
        self.clear_expired_alerts();
```

**File:** util/network-alert/src/alert_relayer.rs (L147-162)
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
```

**File:** util/network-alert/src/alert_relayer.rs (L166-178)
```rust
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
