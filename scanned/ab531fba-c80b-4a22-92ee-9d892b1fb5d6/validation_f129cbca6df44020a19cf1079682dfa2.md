### Title
Expired Alert Replay via Missing Permanent Deduplication — (`util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system deduplicates incoming alerts by `alert_id` using an in-memory `HashMap` (`received_alerts`). When `clear_expired_alerts` is called, expired entries are removed from `received_alerts` without recording the seen IDs anywhere permanent. Because `has_received` only checks `received_alerts` and the `cancel_filter` LRU cache, any unprivileged P2P peer who saved the original alert bytes can replay them after expiry, causing all reachable nodes to re-accept and re-broadcast the old alert with its original valid Foundation signatures.

---

### Finding Description

**Root cause — `clear_expired_alerts` does not preserve seen IDs:** [1](#0-0) 

`clear_expired_alerts` removes expired alerts from `received_alerts` (the HashMap) and `noticed_alerts` (the Vec), but never writes the evicted IDs into `cancel_filter`. After this call, `has_received` returns `false` for those IDs: [2](#0-1) 

**Deduplication check precedes signature verification in the P2P handler:** [3](#0-2) 

The guard at line 147 (`has_received`) is the only thing preventing re-processing. Once an alert expires and is cleared, this guard no longer fires, so the full signature-verification path runs again on the replayed bytes — and passes, because the original Foundation signatures are cryptographically valid forever.

**`clear_expired_alerts` is triggered on every new peer connection:** [4](#0-3) 

Any P2P peer can trigger the cleanup by connecting, then immediately send the saved alert bytes.

**Secondary issue — `cancel_filter` LRU eviction:** [5](#0-4) 

`cancel_filter` is a bounded LRU cache of only 128 entries. After 128 distinct cancel operations, the oldest cancelled IDs are silently evicted, making those IDs replayable as well.

---

### Impact Explanation

An unprivileged P2P peer who captured any previously broadcast alert (all alert bytes are sent in plaintext over the P2P network) can, after the alert's `notice_until` timestamp passes:

1. Reconnect to any CKB node to trigger `clear_expired_alerts`.
2. Send the saved alert bytes via the Alert protocol.
3. The node re-accepts the alert (signature check passes), stores it in `received_alerts`, and re-broadcasts it to all connected peers via `quick_filter_broadcast`. [6](#0-5) 

The result is network-wide re-display of a stale emergency message (e.g., "critical bug — upgrade immediately") that the Foundation already considered resolved. This constitutes an integrity failure in the alert trust model: the signed authorization (Foundation multi-sig) is reusable indefinitely after expiry, analogous to the NFT whitelist code being reusable by any observer.

---

### Likelihood Explanation

- **Attacker preconditions:** None beyond being a P2P peer. Alert bytes are broadcast to all connected peers in plaintext; any node operator or passive observer can save them.
- **Trigger:** Wait for `notice_until` to pass (alerts have finite lifetimes), then connect and send the saved bytes.
- **No key material required:** The original Foundation signatures remain valid; the attacker does not need to forge anything.
- **Realistic:** Past alerts (e.g., `id=20230001`) are publicly documented with their full signed bytes. [7](#0-6) 

---

### Recommendation

In `clear_expired_alerts`, record each evicted alert ID into `cancel_filter` before removing it from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    let expired_ids: Vec<u32> = self.received_alerts
        .iter()
        .filter_map(|(id, alert)| {
            let notice_until: u64 = alert.raw().notice_until().into();
            if notice_until <= now { Some(*id) } else { None }
        })
        .collect();
    for id in expired_ids {
        self.received_alerts.remove(&id);
        self.cancel_filter.put(id, ()); // permanently mark as seen
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, replace `cancel_filter`'s bounded `LruCache` with an unbounded `HashSet` (or a size large enough to cover all realistic alert IDs) to prevent eviction-based replay of cancelled alerts. [8](#0-7) 

---

### Proof of Concept

1. Observe any live CKB alert broadcast on the P2P network; save the raw `packed::Alert` bytes (e.g., the `id=20230001` alert whose bytes are in the test suite).
2. Wait until the alert's `notice_until` timestamp has passed.
3. Connect to a CKB node as a P2P peer (no credentials required).
4. Send the saved bytes on the Alert protocol channel.
5. Observe: `has_received(20230001)` returns `false` (cleared by `clear_expired_alerts`); `verify_signatures` passes (original Foundation sigs); the alert is stored and re-broadcast to all peers.
6. All connected nodes display the expired emergency message. [9](#0-8) [10](#0-9)

### Citations

**File:** util/network-alert/src/notifier.rs (L9-14)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
```

**File:** util/network-alert/src/notifier.rs (L124-132)
```rust
    /// Cancel alert id
    pub fn cancel(&mut self, cancel_id: u32) {
        self.cancel_filter.put(cancel_id, ());
        self.received_alerts.remove(&cancel_id);
        self.noticed_alerts.retain(|a| {
            let id: u32 = a.raw().id().into();
            id != cancel_id
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

**File:** util/network-alert/src/alert_relayer.rs (L86-95)
```rust
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

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L58-92)
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
```
