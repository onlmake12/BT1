### Title
Cancelled Alert Replay via Bounded LRU `cancel_filter` Eviction Allows Undoing Alert Cancellation - (File: `util/network-alert/src/notifier.rs`)

---

### Summary

The `Notifier` in CKB's network-alert subsystem tracks cancelled alert IDs in a bounded LRU cache (`cancel_filter`, size 128). Once more than 128 alerts have been cancelled, the oldest cancelled IDs are silently evicted from the LRU. After eviction, the deduplication guard `has_received()` returns `false` for the evicted ID, allowing any P2P peer to replay the original (publicly broadcast) cancelled alert and re-activate it on the target node — directly undoing the cancellation.

---

### Finding Description

`Notifier` maintains three data structures for alert state:

```
cancel_filter:    LruCache<u32, ()>   // bounded, size 128
received_alerts:  HashMap<u32, Alert>
noticed_alerts:   Vec<Alert>
``` [1](#0-0) 

When a cancel alert arrives, `cancel()` is called:

```rust
pub fn cancel(&mut self, cancel_id: u32) {
    self.cancel_filter.put(cancel_id, ());
    self.received_alerts.remove(&cancel_id);
    self.noticed_alerts.retain(|a| { ... id != cancel_id });
}
``` [2](#0-1) 

This removes the cancelled alert from `received_alerts` and `noticed_alerts`, and records the cancelled ID in `cancel_filter`. The sole replay guard is `has_received()`:

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [3](#0-2) 

This is checked in `alert_relayer.rs` before processing any incoming P2P alert:

```rust
if self.notifier.lock().has_received(alert_id) {
    return;
}
``` [4](#0-3) 

**The defect**: `cancel_filter` is an `LruCache` with a hard-coded capacity of 128:

```rust
const CANCEL_FILTER_SIZE: usize = 128;
``` [5](#0-4) 

Once 128 distinct alert IDs have been cancelled, the LRU evicts the oldest entry. After eviction of cancelled ID `X`:
- `received_alerts.contains_key(X)` → `false` (removed by `cancel()`)
- `cancel_filter.contains(X)` → `false` (evicted from LRU)
- Therefore `has_received(X)` → `false`

The original alert A (id=X) was broadcast publicly over the P2P network with its valid Nervos Foundation signatures. Any peer that observed it can now re-send it. It will pass both the `has_received` guard and `verify_signatures`, and `notifier.add()` will re-insert it into `received_alerts` and `noticed_alerts`, fully undoing the cancellation.

Note that `clear_expired_alerts()` does **not** touch `cancel_filter`:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| { notice_until > now });
    self.noticed_alerts.retain(|a| { notice_until > now });
    // cancel_filter is NOT cleared
}
``` [6](#0-5) 

This means the only mechanism that removes entries from `cancel_filter` is LRU eviction — making the bounded size the sole root cause.

---

### Impact Explanation

Any P2P peer can re-activate a previously cancelled network alert on any node whose `cancel_filter` has evicted the corresponding ID. The re-activated alert will:
- Reappear in `noticed_alerts` and be surfaced to users via `get_blockchain_info().alerts`
- Be re-broadcast to all connected peers (since `mark_as_known` is per-peer and the alert is treated as new)
- Potentially trigger version-based node behavior if the alert carries `min_version`/`max_version` filters

The cancellation mechanism — which exists precisely to retract erroneous or resolved alerts — can be silently bypassed.

---

### Likelihood Explanation

**Precondition**: 128 distinct alert IDs must have been cancelled after the target alert was cancelled, to evict its ID from the LRU. Since each cancel alert requires 2-of-4 Nervos Foundation signatures, only the Foundation can create the precondition. In practice the Foundation has sent very few alerts (e.g., id=20230001 with `cancel=0`), making the precondition unlikely in the near term.

**Exploitation**: Once the precondition is met, exploitation requires zero privilege. The original alert and its signatures were broadcast publicly over P2P. Any peer — including an external attacker who simply observed the network — can replay the raw bytes. The P2P handler in `alert_relayer.received()` has no expiry check (unlike the RPC path), so even an expired-but-not-yet-cleared alert can be replayed this way.

Likelihood is **low** today but the vulnerability is structurally permanent and worsens as the network ages and more alerts are issued.

---

### Recommendation

Replace the bounded `LruCache` for `cancel_filter` with an unbounded `HashSet<u32>`. Cancelled alert IDs are small (u32) and the total number of alerts ever issued is expected to remain in the dozens, making unbounded storage trivially cheap:

```rust
// Before
cancel_filter: LruCache<u32, ()>,

// After
cancel_filter: HashSet<u32>,
```

Alternatively, if memory bounds are required, use a size large enough to exceed any realistic alert issuance count (e.g., 65536), or persist the cancel set across restarts.

---

### Proof of Concept

**Setup**: Node N has processed alerts with IDs 1–129, where:
- Alert id=1 was cancelled by alert id=2 (cancel=1)
- Alerts id=3 through id=130 each have `cancel > 0` (cancelling dummy IDs 200–327)

After processing alert id=130, the LRU evicts id=1 from `cancel_filter` (oldest entry).

**State after eviction**:
- `received_alerts`: does not contain id=1 (removed by `cancel()` when alert id=2 arrived)
- `cancel_filter`: does not contain id=1 (evicted by LRU)
- `has_received(1)` → `false`

**Replay**:
An attacker (any P2P peer) sends the original alert id=1 bytes (publicly observed from the network) to node N.

In `alert_relayer.received()`:
1. `has_received(1)` → `false` → does **not** return early [4](#0-3) 
2. `verify_signatures(&alert)` → `Ok(())` (original valid signatures) [7](#0-6) 
3. `notifier.add(&alert)` → alert id=1 re-inserted into `received_alerts` and `noticed_alerts` [8](#0-7) 
4. Alert id=1 is re-broadcast to all connected peers [9](#0-8) 

The cancellation of alert id=1 is fully undone. The integration test at `test/src/specs/alert/alert_propagation.rs` line 108–130 confirms the intended behavior is that a cancelled alert "should be ignored by all nodes" — this guarantee breaks once the LRU evicts the cancelled ID. [10](#0-9)

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

**File:** util/network-alert/src/alert_relayer.rs (L147-149)
```rust
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L151-162)
```rust
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

**File:** util/network-alert/src/alert_relayer.rs (L166-176)
```rust
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
```

**File:** util/network-alert/src/alert_relayer.rs (L178-178)
```rust
        self.notifier.lock().add(&alert);
```

**File:** test/src/specs/alert/alert_propagation.rs (L108-130)
```rust
        // send canceled alert again, should ignore by all nodes
        node0.rpc_client().send_alert(alert.into());
        let ret = wait_until(20, || {
            nodes.iter().all(|node| {
                node.rpc_client()
                    .get_blockchain_info()
                    .alerts
                    .iter()
                    .all(|a| Into::<u32>::into(a.id) != id1)
            })
        });
        assert!(ret, "Alert should be relayed, but not");
        let alerts = node0.rpc_client().get_blockchain_info().alerts;
        assert_eq!(
            alerts.len(),
            1,
            "All nodes should receive the alert, but not"
        );
        assert_eq!(
            alerts[0].message, warning2,
            "Alert message should be {}, but got {}",
            "alert is canceled", alerts[0].message
        );
```
