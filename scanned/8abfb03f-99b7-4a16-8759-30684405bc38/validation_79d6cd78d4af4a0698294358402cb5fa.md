### Title
Expired Network Alert Replay via P2P — Missing `notice_until` Check in P2P Handler (`util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB Network Alert system uses a 2-of-4 multi-signature scheme to broadcast critical messages to all nodes. The `notice_until` expiry check is enforced only in the RPC `send_alert` handler, but is entirely absent from the P2P `received()` handler in `AlertRelayer`. Any unprivileged P2P peer can directly inject a previously-broadcast, cryptographically-valid but expired alert over the P2P protocol, causing all reachable nodes to accept, store, and re-relay it — and display it to users.

---

### Finding Description

The `RawAlert` structure contains a `notice_until: Uint64` field (a millisecond timestamp) that is intended to mark when an alert expires. [1](#0-0) 

The RPC handler `AlertRpcImpl::send_alert` correctly enforces this expiry before accepting an alert: [2](#0-1) 

However, the P2P handler `AlertRelayer::received()` performs no such check. It only checks for duplicate IDs and verifies cryptographic signatures, then unconditionally relays and stores the alert: [3](#0-2) 

`Verifier::verify_signatures()` only validates the m-of-n secp256k1 signatures over the `RawAlert` hash — it does not inspect `notice_until`: [4](#0-3) 

`Notifier::add()` also performs no expiry check before inserting the alert into `received_alerts` and triggering `notify_network_alert`: [5](#0-4) 

`clear_expired_alerts()` is only called in `AlertRelayer::connected()` (when a new peer connects), not in `received()`: [6](#0-5) 

A secondary issue compounds this: the `cancel_filter` that tracks cancelled alert IDs is an LRU cache of fixed size 128. If more than 128 alerts are cancelled over the lifetime of the network, old cancelled IDs are silently evicted, allowing replay of previously-cancelled alerts via P2P. [7](#0-6) 

---

### Impact Explanation

An attacker who is any P2P peer and who has retained a previously-broadcast, cryptographically-valid alert (e.g., from a past security incident) can:

1. Re-inject it directly over the P2P Alert protocol after its `notice_until` has passed.
2. Every receiving node accepts it (signatures are still valid), stores it, and re-relays it to all their peers.
3. All nodes display the stale alert to operators and users via `get_blockchain_info`.

This allows an attacker to replay old "critical vulnerability — upgrade immediately" alerts indefinitely, causing operator confusion, unnecessary node shutdowns, or panic-driven decisions based on stale security information. The alert system is specifically designed for high-urgency, trusted communications; undermining its expiry guarantee directly undermines its trustworthiness.

---

### Likelihood Explanation

**Low-to-medium.** The attacker must:
- Be a connected P2P peer (no special privilege required — any node on the network qualifies).
- Possess a previously-broadcast alert with valid 2-of-4 Nervos Foundation signatures.

Past alerts have been broadcast publicly across the entire network and are trivially collectible by any node that was online at the time. The attack requires no key compromise, no majority hashpower, and no social engineering.

---

### Recommendation

1. **In `AlertRelayer::received()`**, add an expiry check immediately after parsing the alert, mirroring the RPC handler:
   ```rust
   let now_ms = ckb_systemtime::unix_time_as_millis();
   let notice_until: u64 = alert.as_reader().raw().notice_until().into();
   if notice_until < now_ms {
       // silently drop or ban peer
       return;
   }
   ``` [8](#0-7) 

2. **In `Notifier::add()`**, add a `notice_until > now` guard before inserting into `received_alerts`, so that even if an expired alert bypasses the relayer check, it is not stored or notified. [9](#0-8) 

3. **Consider replacing the LRU `cancel_filter`** with a persistent or unbounded set, or use a monotonically-increasing alert ID scheme, to prevent cancelled-alert replay after LRU eviction. [10](#0-9) 

---

### Proof of Concept

1. At time T, Nervos Foundation broadcasts a valid alert (id=42, `notice_until=T+86400000`) signed by 2-of-4 keys. All nodes receive and store it.
2. At time T+2 days, the alert expires. `clear_expired_alerts` eventually removes it from all notifiers.
3. Attacker (any P2P peer) retained the raw `packed::Alert` bytes from step 1.
4. Attacker opens a P2P connection to any CKB node and sends those bytes on the Alert protocol channel.
5. `AlertRelayer::received()` is called. `has_received(42)` returns `false` (alert was cleared). Signatures verify successfully. No `notice_until` check is performed.
6. The alert is added to the notifier and re-broadcast to all connected peers.
7. All nodes now display the expired "critical" alert to their operators.

### Citations

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

**File:** util/network-alert/src/alert_relayer.rs (L58-61)
```rust
    fn clear_expired_alerts(&mut self) {
        let now = ckb_systemtime::unix_time_as_millis();
        self.notifier.lock().clear_expired_alerts(now);
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

**File:** util/network-alert/src/verifier.rs (L33-64)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
        let signatures: Vec<Signature> = alert
            .signatures()
            .into_iter()
            .filter_map(
                |sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
                    Ok(sig) => {
                        if sig.is_valid() {
                            Some(sig)
                        } else {
                            debug!("invalid signature: {:?}", sig);
                            None
                        }
                    }
                    Err(err) => {
                        debug!("signature error: {}", err);
                        None
                    }
                },
            )
            .collect();
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
    }
```

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
