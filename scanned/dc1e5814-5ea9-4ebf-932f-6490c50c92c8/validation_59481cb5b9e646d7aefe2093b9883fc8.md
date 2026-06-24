All code claims check out against the actual source. Let me confirm each:

- `clear_expired_alerts()` uses `retain()` on `received_alerts` and `noticed_alerts` but never calls `cancel_filter.put()` for evicted IDs. [1](#0-0) 
- `has_received()` checks only `received_alerts` and `cancel_filter`, so after expiry cleanup, it returns `false` for the evicted ID. [2](#0-1) 
- The P2P `received()` handler checks only `has_received()` and `verify_signatures()` — no `notice_until` guard. [3](#0-2) 
- `clear_expired_alerts()` is called unconditionally in `connected()`. [4](#0-3) 
- The RPC handler does have the expiry guard that the P2P handler lacks. [5](#0-4) 

---

Audit Report

## Title
Expired Alert Replay via P2P — Signed Alert Messages Re-Accepted and Re-Broadcast After Expiry - (File: `util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

## Summary
`clear_expired_alerts()` removes expired alert IDs from `received_alerts` without tombstoning them in `cancel_filter`, causing `has_received()` to return `false` for those IDs. The P2P `received()` handler performs no `notice_until` expiry check, so any unprivileged peer can replay the original signed bytes of an expired alert, causing the node to re-accept, re-broadcast to all connected peers, and re-fire `notify_network_alert` — including re-executing the operator-configured `network_alert_notify_script`.

## Finding Description

**Root cause 1 — `clear_expired_alerts` does not tombstone expired IDs:**

`clear_expired_alerts()` uses `retain()` to silently drop expired entries from `received_alerts` and `noticed_alerts`, but never calls `self.cancel_filter.put(id, ())` for the evicted IDs:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    // noticed_alerts also pruned, but cancel_filter never updated
    ...
}
``` [1](#0-0) 

After this runs, `has_received(id)` returns `false` for the expired ID because neither `received_alerts` nor `cancel_filter` contains it: [2](#0-1) 

**Root cause 2 — P2P `received()` has no expiry check:**

The RPC `send_alert` handler rejects alerts whose `notice_until` is in the past: [5](#0-4) 

The P2P `received()` handler performs no such check — it only calls `has_received()` (which returns `false` for the expired ID) and `verify_signatures()` (which passes because the original signatures remain cryptographically valid): [3](#0-2) 

**Root cause 3 — `clear_expired_alerts` is triggered on every peer connection:**

`clear_expired_alerts()` is called at the start of `connected()`, so any new peer connection resets the deduplication state for expired alerts: [4](#0-3) 

**Exploit flow:**
1. A legitimate alert (ID=X) is broadcast and received by all nodes; stored in `received_alerts`.
2. The alert's `notice_until` passes.
3. Attacker connects to a victim node as a standard P2P peer, triggering `clear_expired_alerts()`. Alert ID=X is removed from `received_alerts`; `has_received(X)` now returns `false`.
4. Attacker sends the original alert bytes over the P2P connection.
5. P2P `received()`: `has_received(X)` → `false` (passes); `verify_signatures()` → `Ok` (signatures still valid); alert is re-broadcast to all connected peers and re-added via `notifier.add()`.
6. `notifier.add()` re-inserts the alert into `received_alerts`/`noticed_alerts` and fires `notify_network_alert`, re-executing `network_alert_notify_script` on every affected node. [6](#0-5) 

The attacker can repeat this cycle indefinitely by disconnecting and reconnecting to re-trigger `clear_expired_alerts()`.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single attacker with a standard P2P connection and the bytes of any previously-observed legitimate alert can cause O(network_size) re-processing events across the entire CKB P2P network, since each re-accepting node re-broadcasts to all its peers. The attacker can repeat this at will (reconnect → replay → reconnect → replay), generating sustained network-wide alert traffic with negligible cost. Additionally, `network_alert_notify_script` is re-executed on every affected node, which can cause operational disruption (automated shutdowns, paging, etc.), and `get_blockchain_info().alerts` will again show the expired alert, misleading monitoring systems.

## Likelihood Explanation

Preconditions are minimal: the attacker needs only a standard P2P connection to any CKB node and the raw bytes of any previously-broadcast legitimate alert (observable on the public network). The trigger condition — alert expiry plus a new peer connection — occurs naturally in any long-running node. No keys, no privilege, and no victim mistakes are required. The attack is repeatable indefinitely.

## Recommendation

1. **In `clear_expired_alerts()`:** Tombstone expired IDs into `cancel_filter` before removing them from `received_alerts`:
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
           self.received_alerts.remove(&id);
           self.cancel_filter.put(id, ()); // tombstone expired IDs
       }
       self.noticed_alerts.retain(|a| {
           let notice_until: u64 = a.raw().notice_until().into();
           notice_until > now
       });
   }
   ```

2. **In the P2P `received()` handler:** Add an explicit `notice_until > now` check before signature verification, mirroring the RPC handler's guard:
   ```rust
   let now_ms = ckb_systemtime::unix_time_as_millis();
   let notice_until: u64 = alert.as_reader().raw().notice_until().into();
   if notice_until < now_ms {
       // optionally ban peer for sending expired alert
       return;
   }
   ```

## Proof of Concept

1. Observe a legitimate alert (ID=42) broadcast on the CKB P2P network. Record its raw bytes.
2. Wait until the alert's `notice_until` timestamp has passed.
3. Connect to a target CKB node as a P2P peer using the Alert protocol. This triggers `clear_expired_alerts()`, removing ID=42 from `received_alerts`. Confirm `has_received(42)` now returns `false`.
4. Send the recorded alert bytes over the P2P connection.
5. Observe: the target node's `received()` handler accepts the alert (`has_received(42)` == `false`, signatures valid), re-broadcasts it to all connected peers, re-adds it to `received_alerts`/`noticed_alerts`, and fires `notify_network_alert`.
6. Observe: `get_blockchain_info().alerts` on the target node again lists the expired alert.
7. Observe: `network_alert_notify_script` is re-executed on the target node.
8. Disconnect and reconnect to repeat the cycle indefinitely.

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
