### Title
P2P Alert Relay Accepts Expired Alerts Without Checking `notice_until` — (`util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network-alert P2P handler (`AlertRelayer::received`) verifies only the cryptographic validity of alert signatures but never checks whether the alert's `notice_until` timestamp has already passed. Any unprivileged P2P peer can replay a previously-broadcast, now-expired alert (whose valid signatures are permanently embedded in the signed data) and cause every receiving node to accept it, store it, notify the local user with a stale security warning, and re-broadcast it to all connected peers.

---

### Finding Description

CKB implements a Bitcoin-style network alert system. Alerts carry a `notice_until` field (a millisecond UTC timestamp) that encodes when the alert expires. The field is part of `RawAlert` and is covered by the multi-signature, so it cannot be altered without invalidating the signatures.

**The RPC entry point correctly enforces expiration.** In `rpc/src/module/alert.rs`, `send_alert` rejects any alert whose `notice_until` is already in the past:

```rust
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [1](#0-0) 

**The P2P entry point does not.** `AlertRelayer::received` in `util/network-alert/src/alert_relayer.rs` performs only three checks before accepting an alert from a peer:

1. UTF-8 validity of string fields (lines 106–119)
2. Deduplication via `has_received` (line 147)
3. Cryptographic signature verification via `verifier.verify_signatures` (line 151) [2](#0-1) 

There is no check that `notice_until` is in the future. After passing signature verification, the handler immediately broadcasts the alert to all connected peers and calls `notifier.add`, which stores it in `received_alerts` and fires `notify_controller.notify_network_alert`: [3](#0-2) 

`Verifier::verify_signatures` itself only calls `verify_m_of_n` on the cryptographic signatures; it contains no temporal check whatsoever: [4](#0-3) 

`Notifier::add` also contains no expiration check; it inserts the alert unconditionally and notifies the user: [5](#0-4) 

The only expiration cleanup is `clear_expired_alerts`, which is called exclusively in the `connected` handler (when a new peer connects), not in `received`: [6](#0-5) 

This means: after `clear_expired_alerts` runs on peer connection, a subsequent P2P message from that same peer carrying an expired alert will re-insert it with no resistance.

Real alert signatures are publicly known (e.g., alert 20230001 signatures appear verbatim in the test suite): [7](#0-6) 

---

### Impact Explanation

An attacker who replays an expired alert via P2P causes every receiving node to:

1. **Store the stale alert** in its in-memory `received_alerts` map.
2. **Notify the local user** via `notify_controller.notify_network_alert` with a false/outdated security warning (e.g., "CKB v0.105.* have bugs. Please upgrade.").
3. **Relay the alert to all connected peers**, propagating the replay network-wide.

Because `has_received` deduplication is in-memory and cleared on restart, the attack can be repeated after any node restart. The `clear_expired_alerts` cleanup is triggered only on peer connection events, not on alert receipt, so a connected peer can re-inject an expired alert immediately after cleanup runs.

---

### Likelihood Explanation

The attack requires no special privilege. Any peer that can establish a P2P connection to a CKB node can send a crafted alert message. The valid signatures for past real alerts are publicly available (embedded in the repository's own test files and historically broadcast across the network). No key material needs to be stolen or forged. The attacker only needs to construct a valid `packed::Alert` binary with a known-good signature and an expired `notice_until`.

---

### Recommendation

Add an expiration check in `AlertRelayer::received` immediately after signature verification, mirroring the check already present in `send_alert`:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // optionally ban or just silently drop
    return;
}
```

Alternatively, move the expiration check into `Verifier::verify_signatures` so that both the RPC and P2P paths share a single enforcement point, eliminating the risk of future divergence.

---

### Proof of Concept

1. Obtain the bytes of the real, now-expired alert 20230001 (signatures are in `util/network-alert/src/tests/generate_alert_signature.rs` lines 73–75; `notice_until` = `1681574400000` ms, which is April 2023).
2. Encode it as a `packed::Alert` molecule binary.
3. Connect to any CKB mainnet/testnet node as a P2P peer supporting the Alert protocol.
4. Send the binary as an Alert protocol message.
5. Observe that the node:
   - Does **not** disconnect or ban the sender.
   - Stores the expired alert in its notifier.
   - Emits a `notify_network_alert` event (user-visible warning about a resolved 2023 bug).
   - Relays the message to all its connected peers. [8](#0-7) [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L144-179)
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

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L73-92)
```rust
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
