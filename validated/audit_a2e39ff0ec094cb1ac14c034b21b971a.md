### Title
Expired Network Alert Replay — Stale Alert Re-Notification and Network-Wide Re-Broadcast by Any Peer - (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network alert system deduplicates received alerts using a `HashMap` keyed by alert ID. When an alert expires, `clear_expired_alerts` removes it from that map without recording the ID in any permanent "seen" set. Because the cryptographic signatures on the original alert remain valid indefinitely, any peer that stored the raw alert bytes can replay the message after expiry, causing every receiving node to re-accept, re-notify, and re-broadcast the stale alert across the entire P2P network.

---

### Finding Description

**Root cause — `notifier.rs`**

`Notifier` tracks received alerts in two structures:

```rust
cancel_filter: LruCache<u32, ()>,   // populated only on explicit cancel
received_alerts: HashMap<u32, Alert>, // the sole dedup store for normal alerts
``` [1](#0-0) 

The deduplication gate used by `AlertRelayer` is:

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [2](#0-1) 

When a new peer connects, `clear_expired_alerts` is called, which silently drops every alert whose `notice_until` timestamp has passed:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
``` [3](#0-2) 

Critically, the expired alert's ID is **never inserted into `cancel_filter`**. After the `retain` call, `has_received(id)` returns `false` for that ID.

**Exploit path — `alert_relayer.rs`**

The `received` handler checks deduplication before signature verification:

```rust
if self.notifier.lock().has_received(alert_id) {
    return;
}
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
``` [4](#0-3) 

After expiry and clearing, `has_received` returns `false`, so the handler proceeds to `verify_signatures`. The original multi-signatures are still cryptographically valid (ECDSA signatures do not expire), so verification passes. The alert is then re-added to `received_alerts` and re-broadcast to all connected peers:

```rust
self.mark_as_known(peer_index, alert_id);
let selected_peers: Vec<PeerIndex> = nc
    .connected_peers()
    .into_iter()
    .filter(|peer| self.mark_as_known(*peer, alert_id))
    .collect();
if let Err(err) = nc.quick_filter_broadcast(...) { ... }
self.notifier.lock().add(&alert);
``` [5](#0-4) 

The `known_lists` LRU cache (size 64) tracks which peers have seen which alert IDs, but this is per-peer and in-memory only. It provides no cross-session or cross-peer deduplication once the alert has been cleared from `received_alerts`. [6](#0-5) 

**Secondary issue:** `cancel_filter` itself is an `LruCache` of size 128. Even for explicitly cancelled alerts, after 128 subsequent cancellations the entry is evicted, re-opening the same replay window for cancelled alerts. [7](#0-6) 

---

### Impact Explanation

An unprivileged peer that stored the raw bytes of any previously broadcast alert can, after that alert's `notice_until` timestamp passes:

1. **Re-trigger user notifications** on every node that receives the replayed message — nodes call `notify_controller.notify_network_alert(alert.clone())` again, surfacing a stale (potentially alarming) message to operators and wallet users.
2. **Flood the P2P network** — each receiving node re-broadcasts to all its peers, causing an O(N) amplification wave of redundant traffic across the entire network.
3. **Repeat indefinitely** — because the alert is cleared again on the next `connected` event, the attacker can replay the same message in every new connection cycle.

The alert system is explicitly designed to communicate critical security advisories. Replaying an old critical alert after it has expired can cause widespread false panic, operator confusion, and unnecessary emergency responses.

---

### Likelihood Explanation

- **Attacker precondition:** None beyond being a normal P2P peer. Any node that was online during the original broadcast already holds the raw alert bytes.
- **Trigger:** Wait for `notice_until` to pass, then connect to any CKB node and send the stored alert message.
- **No keys required:** The attacker reuses the original Nervos Foundation signatures verbatim; no cryptographic material needs to be forged.
- **Automation:** Trivially scriptable; the attacker simply stores the raw `packed::Alert` bytes and replays them on demand.

---

### Recommendation

When `clear_expired_alerts` removes an alert from `received_alerts`, insert its ID into a **permanent** (non-LRU, unbounded or epoch-bounded) seen-set so that `has_received` continues to return `true` for that ID:

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
        self.seen_ids.insert(id); // permanent HashSet<u32>
    }
    self.noticed_alerts.retain(|a| {
        let notice_until: u64 = a.raw().notice_until().into();
        notice_until > now
    });
}
```

Update `has_received` to also check `seen_ids`. Since alert IDs are `u32` and the total number of legitimate alerts is expected to be very small (the system is described as for emergency use only), the memory cost is negligible. Additionally, replace `cancel_filter: LruCache` with a plain `HashSet` to eliminate the secondary eviction-based replay window.

---

### Proof of Concept

1. Run a CKB node and observe it receive a valid network alert (ID=1, `notice_until` = T).
2. Capture the raw `packed::Alert` bytes from the P2P message.
3. Wait until `block_timestamp > T` so the alert expires.
4. Connect a custom peer to the node and send the captured bytes as a `NetworkAlert` protocol message.
5. Observe: the node accepts the message (passes `has_received` and `verify_signatures`), calls `notify_network_alert` again, and re-broadcasts to all its peers — identical to the original broadcast behavior.

### Citations

**File:** util/network-alert/src/notifier.rs (L9-21)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
    /// alerts we received
    received_alerts: HashMap<u32, Alert>,
    /// alerts that self node should notice
    noticed_alerts: Vec<Alert>,
    client_version: Option<Version>,
    notify_controller: NotifyController,
}
```

**File:** util/network-alert/src/notifier.rs (L135-143)
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
```

**File:** util/network-alert/src/notifier.rs (L147-149)
```rust
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L24-31)
```rust
const KNOWN_LIST_SIZE: usize = 64;

/// AlertRelayer
/// relay alert messages
pub struct AlertRelayer {
    notifier: Arc<Mutex<Notifier>>,
    verifier: Arc<Verifier>,
    known_lists: LruCache<PeerIndex, HashSet<u32>>,
```

**File:** util/network-alert/src/alert_relayer.rs (L147-162)
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
```

**File:** util/network-alert/src/alert_relayer.rs (L163-178)
```rust
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
