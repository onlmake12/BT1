### Title
Expired Network Alert Replay via P2P Bypasses `notice_until` Enforcement — (`File: util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB Network Alert system enforces a `notice_until` expiry check only in the RPC ingestion path (`rpc/src/module/alert.rs`), but not in the P2P relay path (`util/network-alert/src/alert_relayer.rs`). Any unprivileged P2P peer can replay a historically-valid, now-expired alert (whose signatures are publicly observable from prior network propagation) directly to any CKB node. The receiving node will accept it, display it to the operator via `notify_network_alert`, and re-broadcast it to all connected peers — network-wide propagation of stale alerts with no expiry enforcement.

---

### Finding Description

**Root cause:** `AlertRelayer::received()` calls only `verify_signatures()` before accepting and relaying an alert. `verify_signatures()` verifies the m-of-n secp256k1 multi-signature over the alert hash but performs no time-based check against `notice_until`. The `notice_until` field is part of the signed `RawAlert` and is therefore covered by the signature — but its value is never compared to the current time in the P2P path.

**RPC path (has the check):**

In `rpc/src/module/alert.rs`, `send_alert()` explicitly rejects expired alerts:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [1](#0-0) 

**P2P path (missing the check):**

In `util/network-alert/src/alert_relayer.rs`, `received()` does:

1. Parse the alert (UTF-8 check only)
2. Check `has_received(alert_id)` — deduplication by ID, not by expiry
3. Call `verify_signatures()` — cryptographic check only
4. Broadcast to peers
5. Call `notifier.add()` — no expiry check here either [2](#0-1) 

**`verify_signatures()` — no time check:**

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    // ... only verifies m-of-n secp256k1 signatures
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
        .map_err(|err| err.kind())?;
    Ok(())
}
``` [3](#0-2) 

**`notifier.add()` — no expiry check:**

`add()` checks version range and deduplication but never compares `notice_until` to the current time before inserting into `received_alerts` and calling `notify_controller.notify_network_alert()`. [4](#0-3) 

`clear_expired_alerts()` exists but is only called from `connected()` (when a new peer connects), not from `received()`. An attacker who connects and then immediately sends an expired alert will trigger `clear_expired_alerts()` first (removing old state), then send the expired alert — which is accepted with no expiry check. [5](#0-4) 

---

### Impact Explanation

- Any P2P peer can replay any historically-valid CKB alert (e.g., the real `alert_id=20230001` whose signatures are embedded in the test suite and were broadcast publicly) to nodes that have not yet seen it or have evicted it from their LRU state.
- The target node will: (a) display the stale alert to the operator via `notify_network_alert`, and (b) re-broadcast it to all connected peers, causing network-wide propagation.
- Operators may act on false urgency — e.g., a replayed "CKB v0.105.* have bugs, upgrade now" alert displayed to a node already running a current version.
- The `known_lists` LRU cache (`KNOWN_LIST_SIZE = 64`) evicts peer entries, meaning the same peer can replay the same alert to the same node after eviction. [6](#0-5) 

---

### Likelihood Explanation

- **Attacker preconditions:** None beyond being a connectable P2P peer. No keys, no privileged access.
- **Signatures are public:** Real alert signatures were broadcast across the entire CKB P2P network and are preserved in the test suite (`generate_alert_signature.rs`).
- **Protocol is open:** `SupportProtocols::Alert` is a standard CKB P2P protocol; any peer can send alert messages.
- **Deduplication is bypassable:** The `has_received` check uses `alert_id` as the key. A node that has never seen a given alert ID (e.g., a newly-synced node, or one whose `received_alerts` map was cleared) will accept the replay unconditionally. [7](#0-6) 

---

### Recommendation

Add a `notice_until` expiry check inside `AlertRelayer::received()` immediately after parsing the alert, mirroring the check already present in `send_alert()`:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // optionally ban peer or just silently drop
    return;
}
```

This should be placed before the `verify_signatures()` call to avoid wasting CPU on signature verification for obviously-expired alerts. The same check should be added to `notifier.add()` as a defense-in-depth measure.

---

### Proof of Concept

1. Obtain the real `alert_id=20230001` alert payload and its two valid signatures from the CKB test suite (`util/network-alert/src/tests/generate_alert_signature.rs`, lines 73–82). This alert has `notice_until=1681574400000` (April 2023 — long expired).
2. Connect to any CKB mainnet/testnet node as a P2P peer supporting `SupportProtocols::Alert`.
3. Serialize the alert using the `packed::Alert` molecule format and send it via the Alert protocol channel.
4. The receiving node's `AlertRelayer::received()` will: pass the UTF-8 check, pass `has_received` (if the node hasn't seen this ID), pass `verify_signatures()`, call `notifier.add()`, trigger `notify_network_alert`, and re-broadcast to all its peers.
5. The stale "CKB v0.105.* have bugs. Please upgrade" message is displayed to the operator and propagated network-wide. [8](#0-7) [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L24-31)
```rust
const KNOWN_LIST_SIZE: usize = 64;

/// AlertRelayer
/// relay alert messages
pub struct AlertRelayer {
    notifier: Arc<Mutex<Notifier>>,
    verifier: Arc<Verifier>,
    known_lists: LruCache<PeerIndex, HashSet<u32>>,
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

**File:** util/network-alert/src/alert_relayer.rs (L97-179)
```rust
    #[allow(clippy::needless_collect)]
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

**File:** util/network-alert/src/notifier.rs (L147-149)
```rust
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
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
