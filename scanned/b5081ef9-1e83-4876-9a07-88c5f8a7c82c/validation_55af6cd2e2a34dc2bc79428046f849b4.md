### Title
Expired Alert Deduplication State Reset Enables P2P Replay of Signed Alert Messages — (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

---

### Summary

`Notifier::clear_expired_alerts()` removes expired alerts from `received_alerts` without recording their IDs in `cancel_filter`. This resets the deduplication state for those alert IDs. Because the P2P `AlertRelayer::received()` handler performs no `notice_until` timestamp validation, any peer that retained the original signed alert bytes can re-inject an expired alert after the deduplication state is cleared, causing the node to accept, re-display, and re-broadcast the stale alert network-wide.

---

### Finding Description

**Root cause — `notifier.rs`**

`Notifier` uses two structures for deduplication:

- `received_alerts: HashMap<u32, Alert>` — stores every alert that has been accepted
- `cancel_filter: LruCache<u32, ()>` — stores IDs of explicitly cancelled alerts

`has_received()` returns `true` only if the ID appears in either structure:

```rust
// util/network-alert/src/notifier.rs:147-149
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```

`clear_expired_alerts()` evicts expired entries from `received_alerts` and `noticed_alerts`, but **never writes the evicted IDs into `cancel_filter`**:

```rust
// util/network-alert/src/notifier.rs:135-144
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

After this call, `has_received(expired_id)` returns `false`, making the node treat the alert as unseen.

**Root cause — `alert_relayer.rs`**

The P2P handler `AlertRelayer::received()` checks `has_received()` for deduplication and verifies signatures, but performs **no validation of `notice_until`**:

```rust
// util/network-alert/src/alert_relayer.rs:144-178
let alert_id = alert.as_reader().raw().id().into();
if self.notifier.lock().has_received(alert_id) {
    return;
}
if let Err(err) = self.verifier.verify_signatures(&alert) {
    nc.ban_peer(...);
    return;
}
// ... broadcast to all peers ...
self.notifier.lock().add(&alert);
```

There is no `notice_until < now` rejection here, unlike the RPC path in `rpc/src/module/alert.rs` (lines 104–110) which does enforce the timestamp. The P2P path is the only externally reachable entry point for peers.

**Exploit flow:**

1. Developer broadcasts Alert A (`id=X`, `notice_until=T`) over P2P. Every connected node stores it in `received_alerts`.
2. Time advances past `T`. On the next peer connection event, `connected()` calls `clear_expired_alerts()`, which removes Alert A from `received_alerts`. Its ID is never written to `cancel_filter`.
3. `has_received(X)` now returns `false` on every node.
4. Any peer that retained the original signed Alert A bytes (trivially available — it was broadcast to all peers) re-sends those bytes on the Alert P2P protocol.
5. The receiving node's `received()` handler: `has_received(X)` → `false`; signature check → passes (signatures are unchanged and valid); no timestamp check → proceeds.
6. The node calls `notifier.add(&alert)`, re-inserts Alert A into `received_alerts` and `noticed_alerts`, and re-broadcasts it to all connected peers.
7. The stale alert propagates network-wide again.

The same path applies to a **cancelled** alert whose ID has been evicted from `cancel_filter` due to the fixed LRU capacity of 128 (`CANCEL_FILTER_SIZE = 128`): after 128 subsequent cancellations, the oldest cancelled ID is silently evicted, and the corresponding alert can be replayed.

---

### Impact Explanation

Any unprivileged peer connected to the CKB P2P network can replay any previously broadcast, validly-signed alert after it expires. The replayed alert is accepted by the target node, re-inserted into its active alert set, and re-broadcast to all of its peers, causing network-wide propagation of stale or previously-cancelled critical warnings. Users and operators who rely on the alert system for urgent security guidance (e.g., "upgrade immediately", "stop using version X") will receive misleading, outdated instructions. For cancelled alerts (e.g., a false alarm that was retracted), the replay re-surfaces the retracted warning, potentially triggering unnecessary emergency responses across the network.

**Impact: Medium** — alert system integrity is broken; no direct loss of funds or consensus failure, but the alert system is the designated channel for critical security communications and its integrity is a stated protocol guarantee.

---

### Likelihood Explanation

**Likelihood: Medium.** The attacker requires only:
1. A P2P connection to any CKB node (no privileged access).
2. The raw bytes of a previously broadcast signed alert (trivially retained by any node that was online during the original broadcast, or obtainable from any such peer).

The expiry-based trigger is automatic and predictable: every alert has a finite `notice_until`, so every alert eventually becomes replayable. No special timing or race condition is required.

---

### Recommendation

1. In `clear_expired_alerts()`, record each evicted alert ID in `cancel_filter` before removing it from `received_alerts`, so `has_received()` continues to return `true` for expired IDs:

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

2. As a defence-in-depth measure, add a `notice_until` check in `AlertRelayer::received()` (mirroring the RPC path) to reject and ban peers that send expired alerts.

---

### Proof of Concept

**Setup:** Two nodes, Node A and Node B. Node B retains the raw bytes of a signed alert.

1. Developer sends Alert (`id=1`, `notice_until=now+60_000ms`) via `send_alert` RPC on Node A. Node A broadcasts it; Node B receives and stores it.
2. Wait 60 seconds. Node A's `clear_expired_alerts()` fires (triggered on next peer connection), removing `id=1` from `received_alerts`. `has_received(1)` → `false`.
3. Node B opens a new P2P connection to Node A on the Alert protocol and sends the original raw alert bytes (retained from step 1).
4. Node A's `AlertRelayer::received()`: `has_received(1)` → `false`; `verify_signatures` → `Ok`; no timestamp check; calls `notifier.add()`; re-broadcasts to all peers.
5. Node A now displays the expired alert again and propagates it to all its connected peers.

The attack requires no developer keys — only the previously broadcast signed bytes, which any peer that was online during step 1 possesses. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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
