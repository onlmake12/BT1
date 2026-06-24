Audit Report

## Title
Expired Alert Replay via P2P Enables Re-broadcast and Re-notification — (`util/network-alert/src/alert_relayer.rs`, `util/network-alert/src/notifier.rs`)

## Summary
`clear_expired_alerts()` removes expired alert IDs from `received_alerts` without inserting them into `cancel_filter`, causing `has_received()` to return `false` for those IDs. The P2P `received()` handler deduplicates solely via `has_received()` with no `notice_until` guard, so any peer can replay the raw bytes of a legitimately signed but expired alert. The node re-verifies the still-valid signatures, re-broadcasts to connected peers, and re-fires `notify_network_alert` — all without any signing key.

## Finding Description

**Root cause — `clear_expired_alerts()` does not retain expired IDs in `cancel_filter`:**

`clear_expired_alerts()` calls `retain` on `received_alerts` and `noticed_alerts` only; it never touches `cancel_filter`. [1](#0-0) 

After removal, `has_received()` returns `false` because neither map contains the ID: [2](#0-1) 

**Trigger — `clear_expired_alerts()` fires on every new peer connection:** [3](#0-2) 

**Unguarded P2P path — `received()` checks only `has_received`, no `notice_until` check:** [4](#0-3) 

**`verify_signatures` is expiry-blind — checks only cryptographic validity:** [5](#0-4) 

Verification passes. The handler re-broadcasts to all connected peers absent from `known_lists`, then calls `notifier.add()`: [6](#0-5) 

**`Notifier::add()` performs no expiry check before inserting and firing `notify_network_alert`:** [7](#0-6) 

The `known_lists` LRU cache is only 64 entries (`KNOWN_LIST_SIZE = 64`). An attacker cycling through peer identities evicts old entries, restoring re-broadcast eligibility for the full peer set. [8](#0-7) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Each replay causes the victim node to re-broadcast the alert to all connected peers not yet in `known_lists`. Peers that have themselves processed `clear_expired_alerts` repeat the same cycle, propagating the replayed alert further across the network. By cycling through peer identities to evict the 64-slot LRU, an attacker with a single P2P connection can sustain repeated re-broadcast waves at negligible cost. Additionally, `notify_network_alert` fires on every replay, generating false critical-alert notifications to all local subscribers.

## Likelihood Explanation

All historical CKB alert bytes are passively observable on the P2P network — no key material is required. The trigger condition (`clear_expired_alerts` clearing the deduplication entry) fires automatically on every new peer connection. The attacker needs only a standard P2P connection and the captured bytes of any previously live alert. The cycle is repeatable indefinitely by connecting/disconnecting a peer to re-trigger `clear_expired_alerts`. Likelihood is **high** for any long-running node that processed at least one alert.

## Recommendation

1. **Add a `notice_until` check in the P2P `received()` handler** before proceeding past the `has_received` gate:
   ```rust
   let notice_until: u64 = alert.as_reader().raw().notice_until().into();
   if notice_until < ckb_systemtime::unix_time_as_millis() {
       nc.ban_peer(peer_index, BAD_MESSAGE_BAN_TIME,
           String::from("send us an expired alert"));
       return;
   }
   ```
2. **Retain expired alert IDs in the deduplication set.** `clear_expired_alerts()` should insert removed IDs into `cancel_filter` so `has_received()` continues to return `true` for IDs that were ever processed.
3. **Add an expiry check in `Notifier::add()`** as defense-in-depth before inserting into `received_alerts` or firing `notify_network_alert`.

## Proof of Concept

```
1. Run a CKB node. Observe alert ID=X arrive via P2P; capture its raw bytes.
2. Wait until notice_until passes.
3. Connect a new peer to the victim node → clear_expired_alerts() removes ID=X
   from received_alerts; has_received(X) now returns false.
4. As a connected peer, send the captured raw alert bytes to the victim node.
5. Observe: has_received(X) == false → verify_signatures passes →
   alert re-broadcast to peers not in known_lists → notify_network_alert fires.
6. Disconnect and reconnect with a new peer identity (evicting known_lists entries
   via the 64-slot LRU), then repeat steps 3–5.
   The cycle repeats indefinitely with no key material.
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

**File:** util/network-alert/src/alert_relayer.rs (L24-24)
```rust
const KNOWN_LIST_SIZE: usize = 64;
```

**File:** util/network-alert/src/alert_relayer.rs (L81-87)
```rust
    async fn connected(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        _version: &str,
    ) {
        self.clear_expired_alerts();
```

**File:** util/network-alert/src/alert_relayer.rs (L144-149)
```rust
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L163-179)
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
