### Title
Expired Network Alert Replay via P2P — Missing `notice_until` Validation in P2P Handler Allows Signature Replay (`util/network-alert/src/alert_relayer.rs`)

---

### Summary

The P2P alert handler in `AlertRelayer::received()` does not validate the `notice_until` field of incoming alerts. After `clear_expired_alerts()` removes an expired alert from the `received_alerts` map, the deduplication guard `has_received()` returns `false` for that alert ID. Any unprivileged P2P peer can then replay the original signed alert bytes verbatim. The replayed alert passes signature verification (the cryptographic signatures are still valid), is re-accepted into `received_alerts` and `noticed_alerts`, triggers `notify_network_alert` (which may execute a configured shell script), and is re-broadcast to all connected peers — propagating network-wide.

---

### Finding Description

**Root cause — asymmetric expiry enforcement between RPC and P2P paths.**

The RPC handler `send_alert` in `rpc/src/module/alert.rs` explicitly rejects alerts whose `notice_until` is in the past:

```rust
// rpc/src/module/alert.rs:104-110
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
```

The P2P handler `AlertRelayer::received()` in `util/network-alert/src/alert_relayer.rs` performs **no such check**. Its only guards are a UTF-8 check, a deduplication check, and a signature check:

```rust
// alert_relayer.rs:144-162
let alert_id = alert.as_reader().raw().id().into();
if self.notifier.lock().has_received(alert_id) {   // ← only guard against replay
    return;
}
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
```

The deduplication guard `has_received()` in `util/network-alert/src/notifier.rs` checks two structures:

```rust
// notifier.rs:147-149
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
```

`received_alerts` is a `HashMap<u32, Alert>`. `clear_expired_alerts()` **removes** expired entries from it:

```rust
// notifier.rs:135-144
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|_id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        notice_until > now
    });
    self.noticed_alerts.retain(|a| { ... });
}
```

`clear_expired_alerts()` is called every time a new peer connects:

```rust
// alert_relayer.rs:81-95
async fn connected(...) {
    self.clear_expired_alerts();   // ← purges expired alerts from received_alerts
    ...
}
```

`cancel_filter` is an LRU cache of size 128 that only tracks **cancelled** alerts, not expired ones. Expired alerts are never added to `cancel_filter`.

**Exploit chain:**

1. A legitimate alert (ID=X, `notice_until`=T) is broadcast and accepted by all nodes.
2. Time passes; `T` is now in the past.
3. A new peer connects to a victim node, triggering `clear_expired_alerts()`. Alert ID=X is removed from `received_alerts`. `has_received(X)` now returns `false`.
4. The attacker (any P2P peer) sends the original signed alert bytes for ID=X to the victim node.
5. `has_received(X)` → `false` (not in `received_alerts`, not in `cancel_filter`).
6. `verify_signatures()` → passes (the ECDSA signatures over the alert hash are still cryptographically valid; they do not commit to a timestamp or nonce).
7. `notifier.add(&alert)` is called: the alert is re-inserted into `received_alerts` and `noticed_alerts`, and `notify_network_alert` fires — potentially executing the operator-configured `network_alert_notify_script` with the alert message as an argument.
8. The alert is re-broadcast to all connected peers, propagating the replay network-wide.

The attacker needs only the raw bytes of any previously-broadcast alert, which are observable on the public P2P network.

---

### Impact Explanation

- **Network-wide propagation of expired alerts**: Every node that receives the replayed alert re-broadcasts it to its peers. The replay spreads across the entire CKB P2P network.
- **Re-execution of `network_alert_notify_script`**: Nodes configured with `network_alert_notify_script` will re-execute that script with the alert message as an argument. Depending on operator configuration, this can trigger automated responses (e.g., paging, service restarts, external API calls) based on a stale or attacker-chosen alert.
- **False alarm / operator confusion**: `noticed_alerts` is returned by `get_blockchain_info`. Operators and monitoring systems will see an expired alert re-appear as active, causing confusion and potentially incorrect incident response.
- **Repeated triggering**: The attack can be repeated indefinitely. Each time a new peer connects and `clear_expired_alerts()` runs, the window reopens. An attacker maintaining a persistent connection can time reconnections to continuously re-inject the expired alert.

---

### Likelihood Explanation

- **Attacker preconditions**: None beyond being a P2P peer (no keys, no privileged access). Alert bytes are broadcast publicly on the P2P network and trivially captured.
- **Trigger**: Requires waiting for an alert to expire and for `clear_expired_alerts()` to run (triggered by any new peer connection, a routine event).
- **Repeatability**: The attack window reopens on every peer connection event. An attacker can force this by connecting and disconnecting repeatedly.
- **Historical alerts**: CKB has issued real network alerts (e.g., alert ID 20230001 with hardcoded signatures visible in the test suite). These bytes are permanently available.

Likelihood is **medium-high**: the preconditions are trivially met by any P2P participant, and the trigger condition (peer connection) is a normal network event.

---

### Recommendation

Add an expiry check in `AlertRelayer::received()` immediately after parsing the alert, mirroring the check already present in the RPC handler:

```rust
// In alert_relayer.rs, after parsing the alert (after line 143):
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    // Optionally ban the peer for sending an expired alert
    return;
}
```

Additionally, consider adding expired alert IDs to `cancel_filter` inside `clear_expired_alerts()` so that `has_received()` continues to return `true` for them even after expiry, providing a persistent replay barrier without requiring the expiry check in the P2P handler.

---

### Proof of Concept

**Setup**: Two CKB nodes, A (victim) and B (attacker). A real alert with ID=1 was previously broadcast and has since expired.

1. Attacker captures the raw Molecule-encoded bytes of alert ID=1 from P2P traffic (or from the test suite: the hardcoded signatures for alert 20230001 in `util/network-alert/src/tests/generate_alert_signature.rs` are permanently valid).

2. Attacker connects a new peer to node A. This triggers `AlertRelayer::connected()` → `clear_expired_alerts()` → alert ID=1 is removed from `received_alerts`. `has_received(1)` now returns `false`.

3. Attacker sends the captured alert bytes to node A over the Alert protocol.

4. Node A's `AlertRelayer::received()`:
   - Parses the alert successfully.
   - `has_received(1)` → `false`. Does not return early.
   - `verify_signatures()` → `Ok(())`. Signatures are valid.
   - Calls `notifier.add(&alert)`: alert re-enters `received_alerts` and `noticed_alerts`; `notify_network_alert` fires.
   - Broadcasts the alert to all of node A's connected peers.

5. All peers of node A repeat step 4, propagating the replay network-wide.

6. `node_A.rpc_client().get_blockchain_info().alerts` now shows the expired alert as active. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** util/network-alert/src/alert_relayer.rs (L97-149)
```rust
    #[allow(clippy::needless_collect)]
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let alert: packed::Alert = match packed::AlertReader::from_slice(&data) {
            Ok(alert) => {
                if alert.raw().message().is_utf8()
                    && alert
                        .raw()
                        .min_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                    && alert
                        .raw()
                        .max_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                {
                    alert.to_entity()
                } else {
                    info!(
                        "A malformed message fromP peer {} : not utf-8 string",
                        peer_index
                    );
                    nc.ban_peer(
                        peer_index,
                        BAD_MESSAGE_BAN_TIME,
                        String::from("send us a malformed message: not utf-8 string"),
                    );
                    return;
                }
            }
            Err(err) => {
                info!("A malformed message from peer {}: {:?}", peer_index, err);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
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

**File:** rpc/src/module/alert.rs (L102-110)
```rust
    fn send_alert(&self, alert: Alert) -> Result<()> {
        let alert: packed::Alert = alert.into();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until < now_ms {
            return Err(RPCError::invalid_params(format!(
                "Expected `params[0].notice_until` in the future (> {now_ms}), got {notice_until}",
            )));
        }
```
