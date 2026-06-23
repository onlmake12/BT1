### Title
Expired Network Alert Replay via P2P — Missing Expiry Check in P2P Handler Allows Re-Injection of Expired Signed Alerts (`util/network-alert/src/alert_relayer.rs`, `util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system uses a signed-message model analogous to the ERC1155 voucher: a `RawAlert` is signed by 2-of-4 Nervos Foundation keys and carries a `notice_until` expiry timestamp. The deduplication guard (`has_received`) is backed by `received_alerts: HashMap<u32, Alert>`, which is **purged of expired entries** by `clear_expired_alerts`. The P2P receive handler (`AlertRelayer::received`) does **not** check `notice_until`, so any unprivileged P2P peer who captured the original alert bytes can replay them after expiry, causing every receiving node to re-accept, re-store, and re-broadcast the old alert to all its peers.

---

### Finding Description

**Root cause — two cooperating defects:**

**Defect 1 — `clear_expired_alerts` removes entries from `received_alerts` without adding them to `cancel_filter`.**

`util/network-alert/src/notifier.rs`, lines 135–144:
```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    // cancel_filter is NOT updated here
    ...
}
```
After this call, `has_received(id)` returns `false` for the evicted ID:
```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```
The alert ID is now "forgotten" by the deduplication guard.

**Defect 2 — The P2P receive handler never checks `notice_until`.**

`util/network-alert/src/alert_relayer.rs`, lines 144–178:
```rust
let alert_id = alert.as_reader().raw().id().into();
if self.notifier.lock().has_received(alert_id) { return; }   // guard is now bypassed
if let Err(err) = self.verifier.verify_signatures(&alert) {  // passes: sigs are valid
    nc.ban_peer(...); return;
}
// mark + broadcast + add — all execute on the replayed alert
self.notifier.lock().add(&alert);
```
The only expiry check in the entire codebase is in the **RPC** path (`rpc/src/module/alert.rs`, line 106), which is irrelevant for P2P-injected messages.

**`clear_expired_alerts` is triggered on every new peer connection** (`alert_relayer.rs`, line 87), so the attacker can force the clearing by connecting, then immediately inject the replay.

**End-to-end exploit path:**

1. Attacker passively observes a legitimately signed alert (ID=N, `notice_until`=T) propagating over the P2P network and saves the raw bytes.
2. Time advances past T.
3. Attacker opens a TCP connection to any CKB node, triggering `connected` → `clear_expired_alerts` → alert ID=N is evicted from `received_alerts`.
4. Attacker sends the saved alert bytes over the same P2P connection.
5. `has_received(N)` → `false` (evicted). Signature verification → passes (signatures are still cryptographically valid). No `notice_until` check exists.
6. Node calls `notifier.add(&alert)`, re-inserts the alert into `received_alerts` and `noticed_alerts`, fires `notify_network_alert`, and re-broadcasts to all connected peers.
7. Every downstream peer repeats steps 5–6, causing network-wide re-propagation.

---

### Impact Explanation

- Any unprivileged P2P peer can cause every node on the network to re-display an old, expired alert message to operators and users.
- The replayed alert is re-broadcast transitively to all peers, so a single attacker injection propagates network-wide.
- Operators may take unnecessary emergency action (e.g., halting services, emergency upgrades) in response to a stale alert.
- If the attacker replays an alert with the same ID as a pending new legitimate alert before the Foundation sends it, the new alert will be silently dropped by `has_received` on all nodes that received the replay first, suppressing the real emergency notification.

---

### Likelihood Explanation

- Requires no keys, no privileged access, and no majority hashpower.
- The attacker only needs to have been a P2P peer during the original alert broadcast (or obtained the bytes from any public source).
- The trigger condition (alert expiry + peer connection) is fully attacker-controllable: the attacker connects to force `clear_expired_alerts`, then immediately sends the replay.
- Every historical alert ever broadcast is permanently replayable by this method.

---

### Recommendation

1. **In `clear_expired_alerts`**: when removing an expired alert from `received_alerts`, also insert its ID into `cancel_filter` so `has_received` continues to return `true` after expiry.
2. **In `AlertRelayer::received`**: add an explicit `notice_until < now_ms` check mirroring the one already present in the RPC handler (`rpc/src/module/alert.rs`, line 106), and ban the peer if the alert is expired.

---

### Proof of Concept

```
// Step 1: capture alert bytes during live broadcast (any P2P peer)
let saved_bytes: Bytes = /* alert with id=1, notice_until=T, valid 2-of-4 sigs */;

// Step 2: wait until T has passed

// Step 3: connect to target node — triggers clear_expired_alerts, evicts id=1
let conn = tcp_connect(target_node);
ckb_p2p_handshake(&conn, SupportProtocols::Alert);

// Step 4: send the saved bytes — no expiry check in received(), passes has_received + verify
conn.send(saved_bytes);

// Result: node re-adds alert id=1, fires notify_network_alert, re-broadcasts to all peers
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/network-alert/src/notifier.rs (L9-16)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
    /// alerts we received
    received_alerts: HashMap<u32, Alert>,
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

**File:** util/network-alert/src/alert_relayer.rs (L86-88)
```rust
    ) {
        self.clear_expired_alerts();
        for alert in self.notifier.lock().received_alerts() {
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
