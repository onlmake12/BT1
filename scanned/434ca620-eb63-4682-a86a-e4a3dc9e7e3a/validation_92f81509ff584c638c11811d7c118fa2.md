### Title
Expired Network Alert Replay via Missing `notice_until` Check in P2P Handler — (`util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network alert P2P handler (`AlertRelayer::received`) accepts and re-broadcasts any alert that passes signature verification and is not present in the `received_alerts` map or `cancel_filter`. When `clear_expired_alerts` is called, expired alerts are silently evicted from `received_alerts` without being added to `cancel_filter`. This means any unprivileged P2P peer who previously observed a valid, now-expired alert can replay it verbatim: the node will re-accept it, re-add it to `noticed_alerts`, and re-broadcast it to all connected peers. The RPC path enforces a `notice_until` freshness check; the P2P path does not.

---

### Finding Description

**Root cause — asymmetric freshness enforcement**

The RPC entry point `send_alert` in `rpc/src/module/alert.rs` explicitly rejects alerts whose `notice_until` is in the past:

```rust
// rpc/src/module/alert.rs L104-L110
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [1](#0-0) 

The P2P handler `AlertRelayer::received` in `util/network-alert/src/alert_relayer.rs` performs no equivalent check. Its only gate before accepting an alert is the `has_received` deduplication check and signature verification:

```rust
// alert_relayer.rs L147-L162
if self.notifier.lock().has_received(alert_id) {
    return;
}
if let Err(err) = self.verifier.verify_signatures(&alert) {
    ...
    return;
}
``` [2](#0-1) 

**Root cause — `clear_expired_alerts` does not populate `cancel_filter`**

`Notifier::clear_expired_alerts` removes expired alerts from `received_alerts` and `noticed_alerts` but does **not** insert their IDs into `cancel_filter`:

```rust
// notifier.rs L135-L144
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| { ... });
}
``` [3](#0-2) 

`has_received` checks both maps:

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [4](#0-3) 

After `clear_expired_alerts` runs, `has_received(expired_id)` returns `false`. The expired alert is no longer in either map, so the deduplication gate is open.

**`clear_expired_alerts` is triggered by normal network activity**

`clear_expired_alerts` is called every time a new peer connects:

```rust
// alert_relayer.rs L87
self.clear_expired_alerts();
``` [5](#0-4) 

It is also called on every `get_blockchain_info` RPC invocation:

```rust
// rpc/src/module/stats.rs L133-L135
let now = ckb_systemtime::unix_time_as_millis();
let mut notifier = self.alert_notifier.lock();
notifier.clear_expired_alerts(now);
``` [6](#0-5) 

Both are reachable by any unprivileged actor.

---

### Impact Explanation

After an alert's `notice_until` timestamp passes and `clear_expired_alerts` is triggered (by any peer connection or RPC call), any P2P peer holding the original alert bytes can replay them. The receiving node will:

1. Pass the `has_received` gate (returns `false` — alert was evicted from `received_alerts`, never added to `cancel_filter`).
2. Pass signature verification (the signatures are still cryptographically valid).
3. Re-add the alert to `received_alerts` and `noticed_alerts`.
4. Re-broadcast the alert to all connected peers.

The result is network-wide propagation of a stale, expired alert. Users of affected nodes will see an alert message about a critical problem that has already been resolved. Because the re-broadcast propagates to all peers, a single attacker can cause the expired alert to re-appear across the entire CKB P2P network. This is a meaningful integrity failure in the alert system: the system's purpose is to convey accurate, timely critical information, and an unprivileged peer can subvert that by replaying old messages indefinitely.

---

### Likelihood Explanation

The attacker preconditions are minimal:

- **No privileged keys required.** The attacker only needs the raw bytes of a previously broadcast alert, which are visible to every P2P participant.
- **Trigger is automatic.** `clear_expired_alerts` fires on every peer connection and every `get_blockchain_info` call. On a live node, this happens continuously.
- **No race condition.** The attacker can wait for the alert to expire, then connect and immediately send the replayed bytes. The window is permanent once the alert expires.

The only constraint is that the attacker must have observed the alert while it was live. Since alerts are broadcast to all peers, this is trivially satisfied for any peer that was online during the alert's active period.

---

### Recommendation

Add a `notice_until` freshness check in the P2P `received` handler in `util/network-alert/src/alert_relayer.rs`, mirroring the check already present in the RPC handler:

```rust
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
let now_ms = ckb_systemtime::unix_time_as_millis();
if notice_until < now_ms {
    // drop silently or ban peer
    return;
}
```

Additionally, `clear_expired_alerts` in `util/network-alert/src/notifier.rs` should insert evicted alert IDs into `cancel_filter` so that `has_received` continues to return `true` for them, preventing replay even if the freshness check is somehow bypassed:

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
        self.cancel_filter.put(id, ());
        self.received_alerts.remove(&id);
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

---

### Proof of Concept

1. Attacker connects to a CKB node as a normal P2P peer and observes a live alert (id=42, `notice_until=T`) being broadcast. Attacker saves the raw alert bytes.
2. Time advances past `T`. Any subsequent peer connection or `get_blockchain_info` call triggers `clear_expired_alerts`, which removes alert 42 from `received_alerts`. Alert 42 is not in `cancel_filter`.
3. Attacker sends the saved alert bytes to the target node over the P2P alert protocol.
4. `AlertRelayer::received` is called. `has_received(42)` returns `false` (not in `received_alerts`, not in `cancel_filter`). Signature verification passes (signatures are still valid). The alert is re-added to `noticed_alerts` and broadcast to all connected peers.
5. The target node and all its peers now display the expired alert. The attacker can repeat step 3 indefinitely after each `clear_expired_alerts` cycle. [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L87-87)
```rust
        self.clear_expired_alerts();
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

**File:** rpc/src/module/stats.rs (L133-135)
```rust
            let now = ckb_systemtime::unix_time_as_millis();
            let mut notifier = self.alert_notifier.lock();
            notifier.clear_expired_alerts(now);
```
