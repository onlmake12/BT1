### Title
Expired Alert Signature Replay via P2P After `clear_expired_alerts` Evicts Deduplication State — (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

### Summary

The CKB network alert system deduplicates received alerts by alert ID using `received_alerts` (a `HashMap`) and `cancel_filter` (an LRU cache). When `clear_expired_alerts` is called, expired alerts are silently removed from `received_alerts` without being added to `cancel_filter`. This resets the deduplication guard for that alert ID. Because the P2P `received` handler in `AlertRelayer` performs no `notice_until` freshness check of its own, any unprivileged P2P peer can replay the raw bytes of a previously-valid, now-expired alert and have it accepted, re-broadcast network-wide, and re-notified to node operators.

### Finding Description

**Root cause — `clear_expired_alerts` does not tombstone expired IDs** [1](#0-0) 

`clear_expired_alerts` removes expired entries from `received_alerts` but never calls `self.cancel_filter.put(id, ())` for them. After this call, `has_received(id)` returns `false` for every evicted alert ID: [2](#0-1) 

**Trigger — `clear_expired_alerts` is called on every peer connection** [3](#0-2) 

Every time any peer connects, `clear_expired_alerts` is called, opening the replay window for all alerts whose `notice_until` has passed.

**P2P `received` handler has no `notice_until` check** [4](#0-3) 

The handler checks only `has_received(alert_id)` (line 147) and `verify_signatures` (line 151). There is no check that `notice_until` is in the future. Compare this with the RPC path, which does enforce freshness: [5](#0-4) 

The RPC rejects expired alerts; the P2P handler does not.

**Re-acceptance path**

After the replay passes both guards, the handler:
1. Broadcasts the expired alert to all connected peers via `quick_filter_broadcast`
2. Calls `notifier.add(&alert)`, which re-inserts the alert into `received_alerts` and fires `notify_network_alert` [6](#0-5) 

**Alert schema — `notice_until` is part of the signed `RawAlert`** [7](#0-6) 

The `notice_until` field is inside the signed payload, so the attacker cannot modify it; they replay the original bytes verbatim.

**`calc_alert_hash` hashes only `RawAlert` bytes with no domain tag** [8](#0-7) 

There is no domain separator in the hash, so the same signature bytes are valid for any replay of the same `RawAlert`.

### Impact Explanation

An unprivileged P2P peer who observed any past alert (trivially available from any node that relayed it) can, after the alert expires and `clear_expired_alerts` is triggered, replay the exact original bytes to any CKB node. The node will:

- Accept the message (passes deduplication and signature checks)
- Re-broadcast it to all its connected peers, propagating the replay network-wide
- Fire `notify_network_alert`, causing false-alarm notifications to every node operator subscribed to alert events

The attack is repeatable: each time the replayed alert expires and is cleared again, the same bytes can be replayed again. This enables sustained, low-cost disruption of the alert notification system and could be used to desensitize operators to real alerts ("alert fatigue").

### Likelihood Explanation

- **Attacker preconditions:** none beyond being a connectable P2P peer. Alert bytes are public (broadcast to all peers).
- **Trigger:** wait for any alert to expire (alerts have finite `notice_until`), then connect to a node (which calls `clear_expired_alerts`) and send the saved bytes.
- **No cryptographic break required:** the original valid signatures are reused verbatim.
- **Realistic:** any past mainnet alert is permanently replayable after expiry.

### Recommendation

**Short term:** In `clear_expired_alerts`, tombstone each evicted alert ID into `cancel_filter` before removing it from `received_alerts`:

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
        self.cancel_filter.put(id, ());  // tombstone so has_received stays true
        self.received_alerts.remove(&id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

**Short term (defense-in-depth):** Add a `notice_until` freshness check in the P2P `received` handler, mirroring the existing check in `send_alert`:

```rust
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < ckb_systemtime::unix_time_as_millis() {
    // drop silently or ban peer
    return;
}
```

**Long term:** Consider adding a domain separator to `calc_alert_hash` (e.g., a fixed prefix byte or a network-ID field) to prevent any future cross-context signature reuse, consistent with EIP-712-style structured hashing.

### Proof of Concept

1. Observe any CKB mainnet alert broadcast (e.g., alert `20230001` with known signatures already public in the test suite at `util/network-alert/src/tests/generate_alert_signature.rs` lines 73–75).
2. Save the raw Molecule-encoded `Alert` bytes.
3. Wait until `notice_until` (e.g., `1681574400000` ms) has passed.
4. Connect to any CKB node as a P2P peer on the Alert protocol.
5. Trigger `clear_expired_alerts` by causing any peer to connect to the target node (or simply wait for the next organic peer connection).
6. Send the saved bytes over the Alert protocol channel.
7. Observe: `has_received(20230001)` returns `false`; `verify_signatures` passes; the alert is re-broadcast to all peers and `notify_network_alert` fires on every receiving node. [9](#0-8) [1](#0-0)

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

**File:** util/gen-types/schemas/extensions.mol (L445-458)
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

table Alert {
    raw:                        RawAlert,
    signatures:                 BytesVec,
}
```

**File:** util/gen-types/src/extension/calc_hash.rs (L292-300)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(RawAlert, calc_alert_hash);
```
