### Title
Expired Alert Replay via P2P — Signed Alert Messages Can Be Re-Accepted and Re-Broadcast After Expiry - (File: `util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

### Summary

The CKB network alert system contains a replay vulnerability in its P2P handler. When an alert expires, `clear_expired_alerts()` removes its ID from `received_alerts` but does not add it to `cancel_filter`. This causes `has_received()` to return `false` for the expired ID. Because the P2P `received()` handler has no `notice_until` expiry check (unlike the RPC handler), any unprivileged P2P peer can replay the original signed alert bytes, causing the node to re-accept, re-broadcast, and re-notify the expired alert across the entire network.

### Finding Description

**Root cause — `clear_expired_alerts` does not tombstone expired IDs:**

`clear_expired_alerts()` removes expired entries from `received_alerts` (a `HashMap`) and `noticed_alerts` (a `Vec`), but never inserts the evicted IDs into `cancel_filter` (an `LruCache`). [1](#0-0) 

After this runs, `has_received(id)` returns `false` for the expired alert ID: [2](#0-1) 

**Root cause — P2P `received()` handler has no expiry check:**

The RPC `send_alert` handler correctly rejects alerts whose `notice_until` is in the past: [3](#0-2) 

The P2P `received()` handler in `AlertRelayer` performs no such check. It only calls `has_received()` (which now returns `false` for the expired ID) and then `verify_signatures()` (which passes because the original signatures are cryptographically valid): [4](#0-3) 

**Root cause — `clear_expired_alerts` is triggered on every peer connection:**

`clear_expired_alerts()` is called in the `connected()` handler, meaning the deduplication state is cleared whenever any new peer connects: [5](#0-4) 

**Exploit flow:**

1. A legitimate alert (ID=X, signed by Nervos foundation keys) is broadcast and received by all nodes. It is stored in `received_alerts`.
2. The alert's `notice_until` timestamp passes.
3. A new peer connects to a victim node, triggering `clear_expired_alerts()`. Alert ID=X is removed from `received_alerts`. `has_received(X)` now returns `false`.
4. The attacker (any P2P peer) sends the original alert bytes (same ID, same valid signatures) to the victim node.
5. The P2P `received()` handler: `has_received(X)` → `false` (passes); `verify_signatures()` → `Ok` (signatures are still cryptographically valid); the alert is re-broadcast to all connected peers and re-added via `notifier.add()`.
6. `notifier.add()` inserts the expired alert back into `received_alerts` and `noticed_alerts`, and fires `notify_network_alert`: [6](#0-5) 

7. The `network_alert_notify_script` is re-executed on every affected node. [7](#0-6) 

### Impact Explanation

- **Network-wide alert replay:** Any single P2P peer can cause an expired alert to re-propagate across the entire CKB P2P network, since each re-accepting node re-broadcasts to all its peers.
- **Re-execution of `network_alert_notify_script`:** Operators who configure a shell script to run on alert receipt will have it re-triggered by replayed expired alerts. Depending on the script, this can cause operational disruption (e.g., automated node shutdowns, paging, or other responses).
- **Stale alert re-appearance in RPC:** `get_blockchain_info().alerts` will again show the expired alert, misleading users and monitoring systems.
- **Amplification:** Because each node that accepts the replay re-broadcasts it, a single attacker connection can cause O(network_size) re-processing events.

### Likelihood Explanation

The attacker preconditions are minimal: connect to any CKB node as a standard P2P peer (no keys, no privilege required) and send the bytes of any previously-observed legitimate alert after its `notice_until` has passed. Historical alert bytes are observable on the public network. The trigger condition (alert expiry + a new peer connection to clear the dedup state) occurs naturally in any long-running node.

### Recommendation

1. **In `clear_expired_alerts()`:** When removing an expired alert ID from `received_alerts`, also insert it into `cancel_filter` to permanently tombstone it:
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
           self.cancel_filter.put(id, ()); // tombstone expired IDs
       }
       self.noticed_alerts.retain(|a| {
           let notice_until: u64 = a.raw().notice_until().into();
           notice_until > now
       });
   }
   ```
2. **In the P2P `received()` handler:** Add an explicit `notice_until > now` check before signature verification, mirroring the RPC handler's guard.

### Proof of Concept

```
1. Observe a legitimate alert (ID=42) broadcast on the CKB P2P network. Record its raw bytes.
2. Wait until the alert's notice_until timestamp has passed.
3. Connect to a target CKB node as a P2P peer using the Alert protocol.
4. The connection triggers clear_expired_alerts() on the target node, removing ID=42 from received_alerts.
5. Send the recorded alert bytes over the P2P connection.
6. Observe: the target node's received() handler accepts the alert (has_received(42) == false, signatures valid), re-broadcasts it to all its peers, re-adds it to received_alerts/noticed_alerts, and fires notify_network_alert.
7. Observe: get_blockchain_info().alerts on the target node again lists the expired alert.
8. Observe: network_alert_notify_script is re-executed on the target node.
``` [8](#0-7) [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L24-44)
```rust
const KNOWN_LIST_SIZE: usize = 64;

/// AlertRelayer
/// relay alert messages
pub struct AlertRelayer {
    notifier: Arc<Mutex<Notifier>>,
    verifier: Arc<Verifier>,
    known_lists: LruCache<PeerIndex, HashSet<u32>>,
}

impl AlertRelayer {
    /// Init
    pub fn new(
        client_version: String,
        notify_controller: NotifyController,
        signature_config: NetworkAlertConfig,
    ) -> Self {
        AlertRelayer {
            notifier: Arc::new(Mutex::new(Notifier::new(client_version, notify_controller))),
            verifier: Arc::new(Verifier::new(signature_config)),
            known_lists: LruCache::new(KNOWN_LIST_SIZE),
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

**File:** notify/src/lib.rs (L402-441)
```rust
    fn handle_notify_network_alert(&self, alert: Alert) {
        trace!("Network alert event {:?}", alert);
        let alert_timeout = self.timeout.alert;
        let message = alert
            .as_reader()
            .raw()
            .message()
            .as_utf8()
            .expect("alert message should be utf8")
            .to_owned();
        // notify all subscribers
        for subscriber in self.network_alert_subscribers.values() {
            let subscriber = subscriber.clone();
            let alert = alert.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(alert, alert_timeout).await {
                    error!("Failed to notify network_alert, error: {}", e);
                }
            });
        }

        // notify script
        if let Some(script) = self.config.network_alert_notify_script.clone() {
            let script_timeout = self.timeout.script;
            self.handle.spawn(async move {
                let args = [message];
                match timeout(script_timeout, Command::new(&script).args(&args).status()).await {
                    Ok(ret) => match ret {
                        Ok(status) => {
                            debug!("the network_alert_notify script exited with: {}", status)
                        }
                        Err(e) => error!(
                            "failed to run network_alert_notify_script: {} {}, error: {}",
                            script, args[0], e
                        ),
                    },
                    Err(_) => ckb_logger::warn!("network_alert_notify_script {} timed out", script),
                }
            });
        }
```
