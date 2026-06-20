### Title
Expired Alert Replay via P2P — Lack of Nonce/Revocation in Alert Signature Hash (`util/network-alert/src/notifier.rs`)

---

### Summary

The CKB network alert system signs `RawAlert` messages with a multi-sig from Nervos Foundation keys. When an alert expires, `clear_expired_alerts()` removes it from `received_alerts` but never adds its ID to `cancel_filter`. Because `has_received(id)` checks only those two structures, the ID becomes "unknown" again. An unprivileged P2P peer who saved the original alert bytes can replay them after expiry; the P2P relay path performs no `notice_until` freshness check, so the alert passes signature verification, is re-inserted into `received_alerts`, triggers `notify_network_alert`, and is re-broadcast to all connected peers.

---

### Finding Description

**Root cause — `clear_expired_alerts` does not tombstone expired IDs**

`Notifier::clear_expired_alerts` removes expired entries from `received_alerts` and `noticed_alerts` but never calls `cancel_filter.put(id, ())`: [1](#0-0) 

After this runs, `has_received(id)` returns `false` for the expired alert: [2](#0-1) 

**Root cause — P2P relay path has no `notice_until` freshness check**

The RPC `send_alert` documents that `notice_until` must be in the future: [3](#0-2) 

But the P2P `received` handler in `AlertRelayer` performs only `has_received` + `verify_signatures` — no timestamp check: [4](#0-3) 

**Root cause — `Notifier::add` also has no `notice_until` check**

`add()` inserts the alert into `received_alerts`, calls `notify_network_alert`, and pushes it into `noticed_alerts` without verifying the timestamp: [5](#0-4) 

**`RawAlert` schema — signed fields include `notice_until` but no per-issuance nonce** [6](#0-5) 

`calc_alert_hash` hashes the entire `RawAlert` slice, so the signature is bound to the specific `notice_until` value but there is no monotonic nonce or per-issuance counter that would let the Foundation invalidate a specific past signature without issuing a new cancel alert: [7](#0-6) 

**Secondary issue — `cancel_filter` LRU overflow**

Even for explicitly cancelled alerts, the tombstone store is a bounded `LruCache` of only 128 entries: [8](#0-7) 

Once more than 128 cancel operations have been issued over the network's lifetime, the oldest cancelled IDs are silently evicted, and those alerts become replayable by the same mechanism.

---

### Impact Explanation

An unprivileged P2P peer who captured a valid alert during its original broadcast can replay it after expiry. The replayed alert:

1. Passes `has_received` (ID no longer in `received_alerts` or `cancel_filter`).
2. Passes `verify_signatures` (Foundation signatures remain cryptographically valid forever).
3. Triggers `notify_network_alert`, surfacing the outdated message to all RPC subscribers and node operators.
4. Is re-broadcast to all connected peers via `quick_filter_broadcast`, propagating network-wide.

Node operators who receive a replayed "critical bug — upgrade immediately" alert may halt nodes, perform unnecessary upgrades, or take other disruptive actions based on false information. The attacker needs no keys and no privileged access — only a saved copy of a previously broadcast alert.

**Impact: Medium** — integrity of the alert notification system; potential for network-wide operator disruption.

---

### Likelihood Explanation

Every alert is broadcast to all peers during its active window. Any node (or passive observer) that was connected at that time has the raw bytes. After `notice_until` passes and `clear_expired_alerts` runs (called on every `connected` and periodic `notify` event), the ID is no longer protected. The attacker's only requirement is patience and a saved packet capture. No special tooling is needed beyond a standard P2P client that can send a raw molecule-encoded `Alert` message.

**Likelihood: Medium** — trivially executable by any past observer of the network; the only constraint is that the target node must have already run `clear_expired_alerts` since the alert expired.

---

### Recommendation

1. **Tombstone expired IDs**: In `clear_expired_alerts`, add each removed alert's ID to `cancel_filter` before dropping it from `received_alerts`:
   ```rust
   self.received_alerts.retain(|id, alert| {
       if alert.raw().notice_until() <= now {
           self.cancel_filter.put(*id, ());
           false
       } else {
           true
       }
   });
   ```

2. **Add `notice_until` freshness check in the P2P relay path**: In `AlertRelayer::received`, reject alerts whose `notice_until` is already in the past before calling `notifier.add`.

3. **Increase or make `cancel_filter` unbounded** (or use a persistent set): The 128-entry LRU is too small for a long-lived network; use a `HashSet` or a larger LRU to prevent cancelled-ID eviction.

4. **Include a monotonic per-signer nonce in `RawAlert`**: Analogous to the M-02 recommendation, a nonce field would allow the Foundation to invalidate all prior signatures for a given alert ID without relying solely on the `cancel` mechanism.

---

### Proof of Concept

```
1. Connect to a CKB mainnet/testnet node and observe a valid alert broadcast
   (e.g., alert id=20230001, notice_until=1681574400000).
   Save the raw molecule bytes.

2. Wait until notice_until passes and the node's clear_expired_alerts() fires
   (triggered on next peer connection or periodic notify tick).

3. Open a new P2P connection to any CKB node using the CKB protocol.

4. Send the saved raw Alert bytes as a CKB_Alert protocol message.

5. Observe:
   - The node does NOT return early at has_received() (id no longer in received_alerts).
   - verify_signatures() passes (Foundation signatures are still valid).
   - notifier.add() inserts the alert; notify_network_alert fires.
   - The node re-broadcasts the alert to all its connected peers.
   - RPC get_blockchain_info().alerts now shows the expired alert again.
```

### Citations

**File:** util/network-alert/src/notifier.rs (L9-14)
```rust
const CANCEL_FILTER_SIZE: usize = 128;

/// Notify other module
pub struct Notifier {
    /// cancelled alerts
    cancel_filter: LruCache<u32, ()>,
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

**File:** rpc/src/module/alert.rs (L29-32)
```rust
    /// * [`AlertFailedToVerifySignatures (-1000)`](../enum.RPCError.html#variant.AlertFailedToVerifySignatures) - Some signatures in the request are invalid.
    /// * [`P2PFailedToBroadcast (-101)`](../enum.RPCError.html#variant.P2PFailedToBroadcast) - Alert is saved locally but has failed to broadcast to the P2P network.
    /// * `InvalidParams (-32602)` - The time specified in `alert.notice_until` must be in the future.
    ///
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

**File:** util/gen-types/src/extension/calc_hash.rs (L292-299)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
```
