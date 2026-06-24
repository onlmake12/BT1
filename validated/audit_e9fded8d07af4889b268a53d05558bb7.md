Audit Report

## Title
Expired Alert Replay via P2P — Missing `notice_until` Validation Enables Signature Replay and Network-Wide Flooding — (`util/network-alert/src/alert_relayer.rs`)

## Summary
`AlertRelayer::received()` performs no expiry check on incoming P2P alerts. After `clear_expired_alerts()` removes an expired alert from `received_alerts`, the deduplication guard `has_received()` returns `false` for that alert ID. Any unprivileged P2P peer can replay the original signed alert bytes verbatim: the signatures remain cryptographically valid, the alert is re-accepted, `notify_network_alert` fires, and the alert is re-broadcast to all connected peers — propagating network-wide. The attack can be repeated indefinitely at negligible cost.

## Finding Description

**Root cause — asymmetric expiry enforcement between RPC and P2P paths.**

The RPC handler `send_alert` in `rpc/src/module/alert.rs` explicitly rejects alerts whose `notice_until` is in the past: [1](#0-0) 

The P2P handler `AlertRelayer::received()` in `util/network-alert/src/alert_relayer.rs` performs **no such check**. Its only guards are a UTF-8 check, a deduplication check (`has_received`), and a signature check: [2](#0-1) 

`has_received()` in `notifier.rs` checks two structures — `received_alerts` (a `HashMap`) and `cancel_filter` (an LRU cache of *cancelled* alerts only): [3](#0-2) 

`clear_expired_alerts()` removes expired entries from `received_alerts` and `noticed_alerts`, but **never inserts them into `cancel_filter`**: [4](#0-3) 

`clear_expired_alerts()` is called every time a new peer connects: [5](#0-4) 

**Exploit chain:**
1. A legitimate alert (ID=X, `notice_until`=T) is broadcast and accepted by all nodes.
2. Time passes; T is now in the past.
3. Attacker connects a new peer to victim node → `connected()` → `clear_expired_alerts()` → alert ID=X removed from `received_alerts`. `has_received(X)` → `false`.
4. Attacker sends the original signed alert bytes for ID=X over the Alert protocol.
5. `has_received(X)` → `false` (not in `received_alerts`, not in `cancel_filter`).
6. `verify_signatures()` → `Ok(())` (ECDSA signatures over the alert hash are still valid; they commit to no timestamp or nonce).
7. `notifier.add(&alert)` is called: alert re-enters `received_alerts` and `noticed_alerts`; `notify_network_alert` fires. [6](#0-5) 
8. The alert is re-broadcast to all connected peers, propagating network-wide. [7](#0-6) 

After re-broadcast, the alert is again in `received_alerts` of all reached nodes. The attacker resets the window by disconnecting and reconnecting (triggering another `clear_expired_alerts()`), then replays again — indefinitely.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with only a P2P connection (no keys, no privilege) can repeatedly flood the entire CKB P2P network with replayed alert messages at negligible cost. Each replay propagates to every reachable node, which re-broadcasts to all its peers. The attacker can sustain this by cycling connections to force `clear_expired_alerts()` on victim nodes. Additionally, `noticed_alerts` (returned by `get_blockchain_info`) is polluted with expired alerts, causing false alarms for operators and monitoring systems.

## Likelihood Explanation

Preconditions are trivially met: the attacker needs only to be a P2P peer and possess the raw bytes of any previously-broadcast alert (observable on the public P2P network, and hardcoded in the test suite). The trigger (`clear_expired_alerts()` on peer connection) is a routine network event the attacker can force by connecting and disconnecting. CKB has issued real network alerts with permanently valid signatures. The attack is repeatable indefinitely.

## Recommendation

Add an expiry check in `AlertRelayer::received()` immediately after parsing the alert, mirroring the check already present in the RPC handler:

```rust
// In alert_relayer.rs, after line 143 (after parsing the alert):
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    nc.ban_peer(peer_index, BAD_MESSAGE_BAN_TIME,
        String::from("send us an expired alert"));
    return;
}
```

Additionally, in `clear_expired_alerts()`, insert expired alert IDs into `cancel_filter` before removing them from `received_alerts`, so `has_received()` continues to return `true` for them even after expiry — providing a persistent replay barrier:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until <= now {
            self.cancel_filter.put(*id, ());  // persist replay barrier
            false
        } else {
            true
        }
    });
    // ... noticed_alerts retain unchanged
}
```

## Proof of Concept

1. Capture raw Molecule-encoded bytes of a previously-broadcast alert (e.g., from P2P traffic or from the hardcoded test signatures in `util/network-alert/src/tests/`).
2. Wait for the alert's `notice_until` to pass.
3. Connect a new peer to the victim node. This triggers `AlertRelayer::connected()` → `clear_expired_alerts()` → the expired alert ID is removed from `received_alerts`. Confirm `has_received(id)` returns `false`.
4. Send the captured alert bytes to the victim node over the Alert protocol.
5. Observe: `AlertRelayer::received()` accepts the alert (no expiry check), `verify_signatures()` passes, `notifier.add()` fires `notify_network_alert`, and the alert is re-broadcast to all connected peers.
6. Confirm via `get_blockchain_info` RPC that the expired alert reappears in `alerts`.
7. Disconnect and reconnect to repeat the cycle — demonstrating indefinite repeatability. [8](#0-7) [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L58-61)
```rust
    fn clear_expired_alerts(&mut self) {
        let now = ckb_systemtime::unix_time_as_millis();
        self.notifier.lock().clear_expired_alerts(now);
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L81-88)
```rust
    async fn connected(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        _version: &str,
    ) {
        self.clear_expired_alerts();
        for alert in self.notifier.lock().received_alerts() {
```

**File:** util/network-alert/src/alert_relayer.rs (L144-162)
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
```

**File:** util/network-alert/src/alert_relayer.rs (L165-178)
```rust
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
