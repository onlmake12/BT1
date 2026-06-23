### Title
Expired Alert Replay via P2P Allows Suppression of Active Security Alerts — (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network alert system allows any unprivileged P2P peer to replay a previously-seen, now-expired cancel-alert to silently suppress an active, legitimate security alert on all reachable nodes. The root cause is that `clear_expired_alerts()` removes expired alert IDs from `received_alerts` without adding them to `cancel_filter`, causing `has_received()` to return `false` for those IDs. The P2P receive handler performs no `notice_until` expiry check, so a replayed expired cancel-alert passes all guards, re-executes its `cancel` side-effect, and is re-broadcast network-wide.

---

### Finding Description

**Root cause — `clear_expired_alerts` does not tombstone expired IDs**

`Notifier::clear_expired_alerts()` evicts expired entries from both `received_alerts` and `noticed_alerts`: [1](#0-0) 

It does **not** call `cancel_filter.put(id, ())` for any evicted ID. The deduplication gate is: [2](#0-1) 

After expiry-clearing, `has_received(id)` returns `false` for any evicted alert ID, making the node treat a replayed copy of that alert as brand-new.

**P2P receive path has no `notice_until` guard**

The P2P handler in `AlertRelayer::received()` checks only `has_received` before accepting and re-broadcasting an alert: [3](#0-2) 

There is no check that `alert.raw().notice_until() > now`. Compare this to the RPC path, which does enforce that check: [4](#0-3) 

**`RawAlert` schema contains no nonce or sequence field**

The signed payload is the raw serialization of `RawAlert`, which has no monotonic counter or nonce: [5](#0-4) 

The `id` field is a u32 chosen by the signer, not a monotonically-enforced sequence. Once an alert with a given `id` expires and is evicted, that `id` slot is open for replay.

**`Notifier::add()` executes the `cancel` side-effect unconditionally**

When the replayed cancel-alert is processed, `add()` calls `self.cancel(alert_cancel)` before any version or deduplication check on the target: [6](#0-5) 

`cancel()` removes the target alert from both `received_alerts` and `noticed_alerts`: [7](#0-6) 

**`clear_expired_alerts` is triggered on every new peer connection** [8](#0-7) 

This means the eviction window opens regularly as peers connect and disconnect.

---

### Impact Explanation

**Primary impact — suppression of an active security alert:**

1. Nervos Foundation issues Alert A (`id=1`, `notice_until=far_future`) warning of a critical bug. All nodes display it.
2. Foundation later issues Alert B (`id=2`, `cancel=1`, `notice_until=T_short`) to retract Alert A. Alert B expires and is evicted by `clear_expired_alerts()`.
3. An attacker who observed Alert B on the P2P network replays it to any connected node.
4. The node's `has_received(2)` returns `false` (evicted). Signatures verify (still cryptographically valid). `add()` calls `cancel(1)`, removing Alert A from `received_alerts` and `noticed_alerts`.
5. The replayed Alert B is re-broadcast to all connected peers, propagating the suppression network-wide.

Node operators lose visibility of the active security warning. If Alert A was warning of an exploitable consensus bug, suppressing it increases the window of exposure.

**Secondary impact — re-display of stale alerts:**

Any expired alert (not just cancel-alerts) can be replayed to re-appear in `get_blockchain_info().alerts` and trigger `notify_network_alert` callbacks, causing confusion and false alarms.

---

### Likelihood Explanation

- **No privilege required.** Any P2P peer can send an alert message. The attacker only needs to have observed the original alert, which was broadcast to the entire network.
- **Signatures remain permanently valid.** There is no expiry baked into the signature itself; the signed `RawAlert` hash is static.
- **Eviction is automatic and frequent.** `clear_expired_alerts()` runs on every peer connection event, so the replay window opens as soon as the alert's `notice_until` passes.
- **The attack is silent.** The replayed alert passes all existing checks (UTF-8 validation, signature verification, `has_received` gate) without triggering any ban or error log visible to the victim node.

---

### Recommendation

1. **Tombstone expired IDs in `cancel_filter`.** In `clear_expired_alerts()`, before removing an entry from `received_alerts`, insert its `id` into `cancel_filter`:

   ```rust
   pub fn clear_expired_alerts(&mut self, now: u64) {
       self.received_alerts.retain(|id, alert| {
           let notice_until: u64 = alert.raw().notice_until().into();
           if notice_until <= now {
               self.cancel_filter.put(*id, ()); // tombstone
               false
           } else {
               true
           }
       });
       // noticed_alerts cleanup unchanged
   }
   ```

2. **Add a `notice_until` check in the P2P receive handler**, mirroring the RPC path, so expired alerts are rejected before signature verification.

3. **Consider a monotonic sequence/nonce field in `RawAlert`** so that a cancel-alert for a given target can only be issued once and cannot be replayed after expiry.

---

### Proof of Concept

```
Setup:
  - Node N running CKB with alert system enabled
  - Alert A: id=1, cancel=0, notice_until=far_future, valid 2-of-4 sigs  → active on N
  - Alert B: id=2, cancel=1, notice_until=T_past (already expired), valid 2-of-4 sigs

Step 1: Wait for or trigger clear_expired_alerts() on N
  (happens automatically on any new peer connection)
  → N.notifier.received_alerts no longer contains id=2
  → N.notifier.has_received(2) == false

Step 2: Attacker connects to N as a P2P peer and sends raw Alert B bytes

Step 3: alert_relayer::received() on N:
  - has_received(2) → false  (gate passes)
  - verifier.verify_signatures(&alert_B) → Ok(())  (sigs still valid)
  - notifier.add(&alert_B):
      - alert_cancel = 1 > 0 → self.cancel(1)
      - cancel(1): received_alerts.remove(1), noticed_alerts.retain(id != 1)
      - Alert A is gone from N
  - Alert B is re-broadcast to all of N's peers

Result: Alert A (active security warning) is silently removed from N and
        propagated-removed across the network. Operators see no active alerts.
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

**File:** util/gen-types/schemas/extensions.mol (L445-453)
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
```
