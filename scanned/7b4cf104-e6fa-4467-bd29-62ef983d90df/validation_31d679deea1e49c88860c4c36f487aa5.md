### Title
Expired Alert Replay via Missing Permanent Deduplication in `clear_expired_alerts` - (File: `util/network-alert/src/notifier.rs`)

---

### Summary

The `Notifier::clear_expired_alerts` function removes expired alerts from `received_alerts` and `noticed_alerts` but never records their IDs in `cancel_filter`. After expiry, `has_received(id)` returns `false` for those IDs. Any P2P peer that retained a copy of the original alert (with valid developer signatures) can replay it after expiry. The P2P relay path in `AlertRelayer::received` performs no `notice_until` check, so the replayed alert is accepted, re-added to `noticed_alerts`, and re-broadcast to all connected peers. This is the direct CKB analog of the Shelter.sol M-17 pattern: a state variable that should be permanently "consumed" is instead silently reset, allowing a bypass of the intended one-time-use restriction.

---

### Finding Description

**Root cause — `Notifier::clear_expired_alerts` does not tombstone expired IDs:** [1](#0-0) 

When `clear_expired_alerts` runs, it silently drops expired entries from `received_alerts` and `noticed_alerts`. It never calls `self.cancel_filter.put(id, ())` for those IDs.

**Deduplication gate — `has_received` checks only two stores:** [2](#0-1) 

After expiry, both `received_alerts.contains_key(&id)` and `cancel_filter.contains(&id)` return `false`, so `has_received` returns `false`. The alert ID is now "unknown" again.

**Trigger — `clear_expired_alerts` is called on every new peer connection:** [3](#0-2) 

An attacker connects to a node, triggering the clearing. Immediately after, the attacker sends the expired alert bytes (which it retained from the original broadcast).

**No `notice_until` check in the P2P relay path:** [4](#0-3) 

The `received` handler checks only: (1) UTF-8 validity, (2) `has_received`, (3) signature validity. There is no check that `notice_until > now`. The RPC `send_alert` path does enforce this check: [5](#0-4) 

but the P2P relay path does not, leaving a gap.

**Contrast with the cancel path — which does tombstone:** [6](#0-5) 

`cancel` correctly calls `self.cancel_filter.put(cancel_id, ())`, permanently recording the ID. `clear_expired_alerts` should do the same.

**`cancel_filter` capacity is bounded at 128:** [7](#0-6) 

Even if expired IDs were added to `cancel_filter`, the LRU eviction after 128 entries would eventually allow replay of very old IDs. This is a secondary weakness but is less immediately exploitable.

---

### Impact Explanation

1. **Stale alert display**: Node operators see expired alerts re-appear in `get_blockchain_info().alerts`, causing confusion during incident response.
2. **Network-wide re-broadcast**: The accepting node calls `nc.quick_filter_broadcast` to all connected peers, propagating the replayed alert across the entire P2P network. Each peer that had cleared the alert also re-accepts and re-broadcasts it, creating a network-wide replay storm.
3. **Alert ID squatting**: If the replayed alert's ID matches a new legitimate alert that the CKB team intends to send, the new alert is silently dropped by `has_received` returning `true` on all nodes that accepted the replay. This can suppress a genuine emergency alert.

---

### Likelihood Explanation

- **Attacker requirement**: Any peer that was connected during the original alert broadcast retains the full alert bytes (including valid developer signatures). No key material needs to be stolen or forged.
- **Trigger**: The attacker simply connects to a target node (triggering `clear_expired_alerts`) and then sends the retained alert bytes over the P2P Alert protocol.
- **Persistence**: The attacker can repeat this on reconnect to keep the expired alert alive indefinitely, or rely on the network-wide re-broadcast to propagate it without further effort.
- **No rate limiting or cost**: There is no proof-of-work, stake, or other cost to connecting and sending an alert message.

---

### Recommendation

In `clear_expired_alerts`, tombstone each expired alert ID into `cancel_filter` before removing it from `received_alerts`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until > now {
            true
        } else {
            self.cancel_filter.put(*id, ()); // tombstone expired IDs
            false
        }
    });
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Additionally, add a `notice_until > now` check in `AlertRelayer::received` (the P2P path) to reject expired alerts at the network boundary, mirroring the check already present in the RPC path.

---

### Proof of Concept

1. CKB core team broadcasts alert with `id=42`, `notice_until=T+1h`. All nodes receive and store it.
2. Time advances past `T+1h`. Alert expires.
3. Attacker (any previously connected peer) retains the original alert bytes.
4. Attacker connects to target node → `AlertRelayer::connected` fires → `clear_expired_alerts` removes alert 42 from `received_alerts` and `noticed_alerts`. `cancel_filter` is untouched.
5. Attacker sends the retained alert bytes over the Alert P2P protocol.
6. `AlertRelayer::received`: `has_received(42)` → `false` (cleared). Signatures valid. Alert accepted.
7. `notifier.add(&alert)`: alert 42 re-enters `received_alerts` and `noticed_alerts`.
8. Node broadcasts alert 42 to all connected peers. Each peer repeats steps 4–8.
9. All nodes now display the expired alert. If the CKB team later sends a new alert with `id=42`, it is silently dropped on all nodes because `has_received(42)` now returns `true`.

### Citations

**File:** util/network-alert/src/notifier.rs (L9-9)
```rust
const CANCEL_FILTER_SIZE: usize = 128;
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
