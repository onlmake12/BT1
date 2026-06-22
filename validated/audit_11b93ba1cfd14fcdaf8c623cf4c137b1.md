### Title
Expired Alert Replay via P2P Allows Unauthorized Re-broadcast and Re-notification — (`util/network-alert/src/alert_relayer.rs`, `util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system clears expired alerts from its deduplication store (`received_alerts`) via `clear_expired_alerts()`. Because the P2P `received()` handler only checks `has_received(alert_id)` for deduplication and never validates `notice_until`, any connected P2P peer can replay the raw bytes of a previously valid, now-expired alert. This causes the node to re-verify the (still-valid) signatures, re-add the alert to its state, re-broadcast it to all connected peers, and re-fire the `notify_network_alert` notification — all without holding any signing key.

---

### Finding Description

**Root cause — deduplication state is ephemeral but signatures are permanent.**

`Notifier::clear_expired_alerts()` removes expired alerts from `received_alerts` and `noticed_alerts`: [1](#0-0) 

After this removal, `has_received(alert_id)` returns `false` for the cleared ID: [2](#0-1) 

`clear_expired_alerts()` is triggered every time a new peer connects: [3](#0-2) 

The P2P `received()` handler then checks only `has_received`: [4](#0-3) 

There is **no `notice_until` check** in the P2P path. Compare this to the RPC path in `rpc/src/module/alert.rs`, which does reject expired alerts: [5](#0-4) 

The P2P handler has no equivalent guard. After `has_received` returns `false`, the handler calls `verifier.verify_signatures(&alert)`: [6](#0-5) 

The signatures in the replayed alert are cryptographically valid (they were legitimately signed by Nervos Foundation keys). `verify_signatures` only checks that the signatures match the alert hash — it has no concept of expiry: [7](#0-6) 

Verification passes. The handler then re-broadcasts the alert to all connected peers and calls `notifier.add()`: [8](#0-7) 

`Notifier::add()` also performs no expiry check before inserting into `received_alerts` and firing `notify_network_alert`: [9](#0-8) 

**Exploit path (step by step):**

1. Attacker captures the raw bytes of a legitimately broadcast alert (e.g., alert ID=20230001) while it is live on the P2P network — no key required, just passive observation.
2. The alert's `notice_until` timestamp passes. The next time any peer connects to the victim node, `clear_expired_alerts()` purges the alert from `received_alerts`.
3. Attacker (as a connected P2P peer) sends the captured alert bytes to the victim node.
4. `received()`: `has_received(20230001)` → `false` → proceeds.
5. `verify_signatures` → passes (signatures are still cryptographically valid).
6. Alert is re-broadcast to all peers connected to the victim node.
7. `notifier.add()` re-inserts the alert and fires `notify_network_alert` again.
8. Attacker can repeat indefinitely by triggering `clear_expired_alerts()` (e.g., by connecting/disconnecting a second peer) and re-sending the alert bytes.

---

### Impact Explanation

- **Network spam / amplification**: Each replayed alert is re-broadcast to all connected peers, which in turn may re-broadcast to their peers. A single attacker with one P2P connection can amplify the replay across the entire network.
- **User confusion / false alarm**: Nodes re-fire `notify_network_alert` for an expired alert, causing wallets and monitoring tools that subscribe to alert notifications to display stale or misleading critical warnings.
- **Repeated unauthorized state mutation**: The alert is re-inserted into `received_alerts` and `noticed_alerts` without any authorized re-issuance by the key holders — the same signed bytes produce repeated effects, directly analogous to the airdrop signature replay.
- **No key compromise required**: The attacker is any unprivileged P2P peer who observed the original broadcast.

---

### Likelihood Explanation

All historical CKB alerts are publicly observable on the P2P network. Their raw bytes are trivially captured. The attack requires only a standard P2P connection — no privileged access, no key material, no brute force. The trigger condition (`clear_expired_alerts` clearing the deduplication entry) occurs naturally on every new peer connection. Likelihood is **high** for any node that has been running long enough to have processed at least one alert.

---

### Recommendation

1. **Check `notice_until` in the P2P `received()` handler** before processing, mirroring the existing RPC-side check:
   ```rust
   let notice_until: u64 = alert.as_reader().raw().notice_until().into();
   let now_ms = ckb_systemtime::unix_time_as_millis();
   if notice_until < now_ms {
       // ban or silently drop
       return;
   }
   ```
2. **Do not remove expired alert IDs from the deduplication set.** `clear_expired_alerts()` should retain the alert ID in a separate `expired_ids` set (or keep the ID in `cancel_filter`) so that `has_received(id)` continues to return `true` for IDs that were ever processed, preventing replay after expiry.
3. **Add an expiry check in `Notifier::add()`** as a defense-in-depth measure before inserting into `received_alerts` or firing `notify_network_alert`.

---

### Proof of Concept

```
1. Run a CKB node. Observe alert ID=20230001 arrive via P2P; capture its raw bytes.
2. Wait until `notice_until` (1681574400000 ms) passes.
3. On the next peer connection event, clear_expired_alerts() removes ID=20230001
   from received_alerts.
4. As a connected peer, send the captured raw alert bytes to the node.
5. Observe: has_received(20230001) == false → verify_signatures passes →
   alert re-broadcast to all peers → notify_network_alert fires again.
6. Trigger another clear_expired_alerts() (connect a new peer) and repeat step 4.
   The cycle repeats indefinitely with no key material.
```

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

**File:** util/network-alert/src/alert_relayer.rs (L144-149)
```rust
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L151-162)
```rust
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

**File:** util/network-alert/src/alert_relayer.rs (L163-179)
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
