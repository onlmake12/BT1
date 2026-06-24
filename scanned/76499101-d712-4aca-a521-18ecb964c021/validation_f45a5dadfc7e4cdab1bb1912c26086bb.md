Audit Report

## Title
Expired Alert ID Not Recorded in `cancel_filter` Enables Replay Re-Propagation — (`util/network-alert/src/notifier.rs`)

## Summary
`clear_expired_alerts` removes expired entries from `received_alerts` and `noticed_alerts` but never writes their IDs into `cancel_filter`. After expiry, `has_received(id)` returns `false` for those IDs. Any peer that retained the raw signed bytes of a legitimately-issued alert can reconnect, replay the bytes, pass signature verification (signatures cover content, not timestamp), and cause the victim node to re-accept, re-notify all subscribers, and re-broadcast the alert to every connected peer.

## Finding Description
`Notifier` maintains two deduplication structures:
- `received_alerts: HashMap<u32, Alert>` — live alerts
- `cancel_filter: LruCache<u32, ()>` — permanently suppressed IDs

`has_received` gates every incoming alert at `notifier.rs` L147–149:
```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```

`clear_expired_alerts` at `notifier.rs` L135–144 silently drops expired entries from both `received_alerts` and `noticed_alerts` without touching `cancel_filter`:
```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| { ... });
}
```

By contrast, `cancel` at `notifier.rs` L125–126 correctly writes to `cancel_filter`:
```rust
pub fn cancel(&mut self, cancel_id: u32) {
    self.cancel_filter.put(cancel_id, ());
    ...
}
```

The existing test `test_clear_expired_alerts` at `tests/test_notifier.rs` L101–104 explicitly asserts `!notifier.has_received(1)` after expiry, confirming the deduplication gap is present and untested-against.

In `alert_relayer.rs` L147–178, the `received` handler:
1. Checks `has_received` → returns `false` for the expired-then-cleared ID
2. Calls `verify_signatures` → passes, because signatures cover `calc_alert_hash()` (content only, no timestamp)
3. Broadcasts to all connected peers via `quick_filter_broadcast`
4. Calls `notifier.add(&alert)` → re-inserts into `received_alerts` and `noticed_alerts`, calls `notify_network_alert`

`clear_expired_alerts` is triggered on every new peer connection at `alert_relayer.rs` L87:
```rust
async fn connected(...) {
    self.clear_expired_alerts();
    ...
}
```

This means each new inbound connection resets the deduplication state for all expired alerts, making the window continuously re-openable.

## Impact Explanation
An attacker with stored bytes of any previously valid signed alert can connect to arbitrary nodes, replay the alert, and cause network-wide re-propagation with each victim node broadcasting to all its peers. The fan-out is unbounded across the P2P graph. This fits **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the attacker needs no key material, no hashpower, and no special privilege; only previously observed network traffic and the ability to make P2P connections.

## Likelihood Explanation
Every node online during the original alert broadcast received and could have stored the raw signed bytes. The trigger (`clear_expired_alerts` on peer connect) fires automatically and requires no timing precision. The attack is repeatable indefinitely: connect → replay → disconnect → reconnect. No threshold of colluding nodes is required; a single attacker node suffices to initiate the cascade.

## Recommendation
In `clear_expired_alerts`, record each removed ID into `cancel_filter` before dropping it:
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
This mirrors the existing `cancel` path and permanently suppresses expired IDs, consistent with how explicitly cancelled alerts are handled.

## Proof of Concept
1. Run a victim node. Observe a valid alert with `id=42`, `notice_until=T` broadcast on the network; store the raw bytes.
2. Advance time past `T`.
3. Connect a peer to the victim. `connected()` fires `clear_expired_alerts(now > T)`, removing `id=42` from `received_alerts`. `cancel_filter` is unchanged.
4. Send the stored alert bytes over the P2P alert protocol.
5. Victim: `has_received(42)` → `false` (not in `received_alerts`, not in `cancel_filter`).
6. Victim: `verify_signatures` → `Ok(())` (signatures still valid over original content).
7. Victim: broadcasts to all connected peers; calls `notifier.add(&alert)` → re-inserts `id=42`, calls `notify_network_alert`.
8. Each receiving peer repeats steps 5–7, propagating the stale alert network-wide.
9. Repeat from step 3 to re-trigger indefinitely.

Unit test addition to confirm the gap: after `notifier.add(&alert)`, `notifier.clear_expired_alerts(after_expired_time)`, assert `notifier.has_received(id)` is still `true` — this assertion currently fails, directly reproducing the bug (as shown by the existing test at `tests/test_notifier.rs` L101–104 which asserts the opposite). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** util/network-alert/src/alert_relayer.rs (L87-87)
```rust
        self.clear_expired_alerts();
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

**File:** util/network-alert/src/tests/test_notifier.rs (L101-104)
```rust
    notifier.clear_expired_alerts(after_expired_time);
    assert!(!notifier.has_received(1));
    assert_eq!(notifier.received_alerts().len(), 0);
    assert_eq!(notifier.noticed_alerts().len(), 0);
```
