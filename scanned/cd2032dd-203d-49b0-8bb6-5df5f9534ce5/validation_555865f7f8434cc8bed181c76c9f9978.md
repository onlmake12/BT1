### Title
Expired Network Alerts Accepted and Re-Broadcast via P2P Without `notice_until` Expiry Check — (`File: util/network-alert/src/alert_relayer.rs`)

### Summary
The P2P alert relay handler in `AlertRelayer::received()` verifies only the cryptographic multi-signature of an incoming alert but never checks whether the alert's `notice_until` timestamp has already passed. Any unprivileged peer can replay a historically valid but now-expired signed alert, causing every receiving node to accept it, display it to operators, and re-broadcast it across the entire network.

### Finding Description
CKB's network alert system allows the Nervos Foundation to broadcast signed emergency messages to all nodes. Each alert carries a `notice_until` field (a millisecond UTC timestamp) that is included in the signed payload and defines when the alert expires.

There are two entry points for alert ingestion:

**RPC path** (`rpc/src/module/alert.rs`, `send_alert`): correctly rejects expired alerts before signature verification. [1](#0-0) 

**P2P path** (`util/network-alert/src/alert_relayer.rs`, `AlertRelayer::received`): calls only `self.verifier.verify_signatures(&alert)` and never checks `notice_until`. [2](#0-1) 

The `Verifier::verify_signatures` function performs only cryptographic multi-signature validation; it contains no temporal check whatsoever. [3](#0-2) 

Because the signature covers the `notice_until` value (it is part of `calc_alert_hash()`), a historically valid signature remains cryptographically valid forever. The P2P handler therefore accepts any expired alert whose signatures were once legitimate, adds it to the notifier, and re-broadcasts it to all connected peers. [4](#0-3) 

### Impact Explanation
An attacker who replays an expired alert causes every reachable node to:
1. Accept the stale alert and display it to the node operator (false emergency notification).
2. Re-broadcast it to all connected peers, propagating the stale alert network-wide.

Node operators who see an expired "upgrade immediately" or "critical bug" alert may take unnecessary or harmful actions (e.g., halting operations, downgrading software). The `clear_expired_alerts` housekeeping only runs on the `connected` event, not on `received`, so a replayed alert added via `received` persists in `received_alerts` until the next peer connection event. [5](#0-4) [6](#0-5) 

### Likelihood Explanation
At least one real, fully-signed, now-expired alert exists in the codebase test fixtures (`notice_until = 1681574400000`, April 2023). Any node on the public network that stores historical alert data, or any attacker who captured the alert bytes from the wire, can replay it immediately. No privileged access, key material, or majority hashpower is required — only a P2P connection to a target node. [7](#0-6) 

### Recommendation
Add an expiry check inside `AlertRelayer::received()` immediately after parsing the alert, mirroring the guard already present in the RPC handler:

```rust
let notice_until: u64 = alert.raw().notice_until().into();
let now_ms = ckb_systemtime::unix_time_as_millis();
if notice_until < now_ms {
    debug!("Ignoring expired alert {} from peer {}", alert_id, peer_index);
    return;
}
```

This check should be placed before `verify_signatures` to avoid wasting CPU on signature verification for obviously stale messages. [8](#0-7) 

### Proof of Concept

1. Capture (or reconstruct from test fixtures) the bytes of the real expired alert `id=20230001` with `notice_until=1681574400000` (April 2023) and its two valid Nervos Foundation signatures.
2. Open a P2P connection to any CKB mainnet node using the Alert protocol (`SupportProtocols::Alert`).
3. Send the raw alert bytes directly over the wire.
4. The receiving node's `AlertRelayer::received()` will:
   - Parse the alert successfully.
   - Find `has_received(20230001) == false` (first replay).
   - Call `verify_signatures` → passes (signatures are still cryptographically valid).
   - Broadcast the expired alert to all of its connected peers.
   - Call `notifier.add(&alert)` → the expired alert appears in `get_blockchain_info().alerts` on the target node.
5. Each peer that receives the re-broadcast repeats steps 4, propagating the stale alert across the network. [9](#0-8)

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

**File:** util/network-alert/src/notifier.rs (L134-144)
```rust
    /// Clear all expired alerts
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

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L62-71)
```rust
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
```
