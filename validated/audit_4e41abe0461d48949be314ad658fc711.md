All six cited code references have been verified against the actual repository. Every claim checks out:

- `clear_expired_alerts` at [1](#0-0)  uses `retain` on both maps but never calls `self.cancel_filter.put(id, ())`, confirmed.
- `has_received` at [2](#0-1)  checks only `received_alerts` and `cancel_filter`, both of which are empty for expired IDs, confirmed.
- `AlertRelayer::received` at [3](#0-2)  has no `notice_until` freshness check, confirmed.
- RPC `send_alert` at [4](#0-3)  enforces `notice_until > now`, confirmed.
- `connected` at [5](#0-4)  calls `self.clear_expired_alerts()` on every peer connection, confirmed.
- Unit test at [6](#0-5)  asserts `!notifier.has_received(1)` after expiry, confirmed.

---

Audit Report

## Title
Expired Alert Replay via P2P Due to Missing Tombstone in `clear_expired_alerts` — (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

## Summary
`Notifier::clear_expired_alerts` removes expired alerts from `received_alerts` without inserting their IDs into `cancel_filter`, causing `has_received(id)` to return `false` for any previously-seen expired alert. The P2P receive handler `AlertRelayer::received` performs no `notice_until` freshness check, so any peer who captured the original signed alert bytes can replay them after expiry, causing every node that has cleared the alert to re-accept, re-notify operators, and re-broadcast the stale alert network-wide. The cycle repeats on every subsequent peer connection that triggers `clear_expired_alerts`.

## Finding Description
**Root cause 1 — `clear_expired_alerts` does not tombstone expired IDs:**

`Notifier::clear_expired_alerts` ( [1](#0-0) ) uses `retain` to drop expired entries from `received_alerts` and `noticed_alerts` but never calls `self.cancel_filter.put(id, ())`. The `cancel` method ( [7](#0-6) ) correctly tombstones cancelled IDs into `cancel_filter`; the expiry path does not.

After expiry, `has_received(id)` ( [2](#0-1) ) checks `received_alerts.contains_key(&id) || cancel_filter.contains(&id)` — both are `false`, so the function returns `false`, reopening the acceptance window for that alert ID.

**Root cause 2 — P2P receive path has no freshness check:**

`AlertRelayer::received` ( [3](#0-2) ) checks `has_received(alert_id)` and then calls `verifier.verify_signatures`. There is no check that `alert.raw().notice_until()` is in the future. The RPC `send_alert` path ( [4](#0-3) ) enforces `notice_until > now_ms`, but this guard is absent from the P2P path.

**Exploit cycle:**
1. A legitimate alert (id=N, `notice_until`=T) is broadcast; attacker captures raw bytes from the P2P wire.
2. T elapses. Any new peer connection triggers `clear_expired_alerts` ( [5](#0-4) ), removing alert N from `received_alerts`. `has_received(N)` now returns `false`.
3. Attacker connects as a normal P2P peer and sends the original bytes.
4. `has_received(N)` is `false` → signature verification passes (signature is permanently valid for the same `RawAlert` hash) → `notifier.add(&alert)` re-inserts the alert and fires `notify_network_alert` → node re-broadcasts to all connected peers.
5. The next peer connection triggers `clear_expired_alerts` again, removing the re-inserted alert and reopening the window. The cycle repeats indefinitely at zero additional cost.

## Impact Explanation
An unprivileged attacker can continuously inject stale, legitimately-signed emergency alerts into the CKB P2P network. Each injection triggers a network-wide broadcast, causing all nodes to re-display the stale alert to operators and re-relay it to peers. The self-reinforcing cycle (re-accept → re-broadcast → next connection clears → re-accept) means the attacker can sustain network-wide alert propagation with minimal effort. This constitutes a **High** severity finding matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
Alert bytes are transmitted in plaintext over the P2P protocol and are trivially observable by any participant. `clear_expired_alerts` fires on every new peer connection ( [5](#0-4) ), so the replay window opens naturally during normal node operation without any attacker intervention. No keys, no privileged access, and no special tooling are required beyond a standard P2P client. The attack is repeatable indefinitely.

## Recommendation
1. **In `clear_expired_alerts`**, insert each expired ID into `cancel_filter` before removing it from `received_alerts`, mirroring the `cancel` method:
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
        self.cancel_filter.put(id, ()); // tombstone expired IDs
    }
    self.received_alerts.retain(|_, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```
2. **In `AlertRelayer::received`**, add a `notice_until > now` check mirroring the RPC path, and ban peers that send expired alerts.
3. Consider replacing the bounded `LruCache` (`CANCEL_FILTER_SIZE = 128`) for `cancel_filter` with an unbounded or time-bounded set to prevent eviction-based replay of cancelled or expired alerts.

## Proof of Concept
The existing unit test `test_clear_expired_alerts` in `util/network-alert/src/tests/test_notifier.rs` ( [8](#0-7) ) already proves the replay window opens: after `clear_expired_alerts(after_expired_time)`, `assert!(!notifier.has_received(1))` passes, confirming the ID is no longer blocked. A full integration PoC:
1. Run a CKB node and connect as a P2P peer using the Alert protocol; capture the raw bytes of any broadcast alert with id=N.
2. Wait for `notice_until` to elapse and for any new peer to connect to the target node (triggering `clear_expired_alerts`).
3. Connect to the target node as a new P2P peer and send the captured bytes verbatim.
4. Observe: the node logs `notify_network_alert` for alert N and re-broadcasts to all connected peers.
5. Repeat from step 3 after the next peer connection — no new signatures required.

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

**File:** util/network-alert/src/alert_relayer.rs (L144-162)
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

**File:** util/network-alert/src/tests/test_notifier.rs (L101-104)
```rust
    notifier.clear_expired_alerts(after_expired_time);
    assert!(!notifier.has_received(1));
    assert_eq!(notifier.received_alerts().len(), 0);
    assert_eq!(notifier.noticed_alerts().len(), 0);
```
