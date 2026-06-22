### Title
Expired Network Alert Signature Replay via P2P — (`util/network-alert/src/notifier.rs`, `util/network-alert/src/alert_relayer.rs`)

### Summary

The CKB network alert system uses multi-signature verification to authenticate alerts broadcast over P2P. Once a valid alert expires, `clear_expired_alerts()` removes it from `received_alerts` without adding its ID to `cancel_filter`. The P2P receive handler (`AlertRelayer::received`) has no `notice_until` freshness check. Any unprivileged P2P peer who captured the original alert bytes can replay them after expiry, causing every receiving node to re-accept, re-display, and re-broadcast the stale alert as if it were new.

### Finding Description

**Root cause — `clear_expired_alerts` does not tombstone expired IDs:**

`Notifier::clear_expired_alerts` removes expired alerts from `received_alerts` and `noticed_alerts`, but never inserts the expired ID into `cancel_filter`. [1](#0-0) 

After this call, `has_received(id)` returns `false` for the expired alert ID because neither map contains it: [2](#0-1) 

**Root cause — P2P receive path has no `notice_until` freshness check:**

`AlertRelayer::received` only checks `has_received(alert_id)` and then verifies the cryptographic signature. There is no check that `notice_until` is in the future: [3](#0-2) 

By contrast, the RPC `send_alert` path does enforce `notice_until > now`: [4](#0-3) 

**`clear_expired_alerts` is triggered on every new peer connection:** [5](#0-4) 

**Exploit flow:**

1. A legitimate alert (id=N, `notice_until`=T) is broadcast and accepted by all nodes. The attacker captures the raw signed bytes from the P2P wire.
2. Time passes; `notice_until` elapses. When any new peer connects, `clear_expired_alerts()` is called, removing alert N from `received_alerts`. `has_received(N)` now returns `false`.
3. The attacker connects to any CKB node as a normal P2P peer and sends the original signed bytes.
4. `AlertRelayer::received` sees `has_received(N) == false`, passes signature verification (the signature is still cryptographically valid), calls `notifier.add(&alert)`, which re-inserts the alert and fires `notify_network_alert`.
5. The node re-broadcasts the stale alert to all connected peers, propagating the replay across the network.

**No nonce or sequence number is included in the signed `RawAlert` hash**, so the same signature is permanently valid for the same alert content: [6](#0-5) 

### Impact Explanation

- Any unprivileged P2P peer can re-inject an expired, legitimately-signed alert into the network at will, indefinitely.
- Every node that has cleared the alert will re-accept it, re-display it to operators, and re-broadcast it to peers, causing network-wide propagation of stale emergency warnings.
- Node operators may take unnecessary emergency actions (halting operations, emergency upgrades) in response to a replayed stale alert.
- The attacker needs no keys and no privileged access — only the raw bytes of any previously broadcast alert, which are observable on the P2P network.

### Likelihood Explanation

The `clear_expired_alerts` trigger fires on every new peer connection, so the window opens naturally during normal node operation. Alert bytes are transmitted in plaintext over the P2P protocol and are trivially capturable by any participant. The attack requires no special tooling beyond a standard P2P client.

### Recommendation

1. **In `clear_expired_alerts`**, insert each expired alert ID into `cancel_filter` before removing it from `received_alerts`, so `has_received(id)` continues to return `true` after expiry:

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
        self.cancel_filter.put(id, ()); // tombstone expired IDs
    }
    self.received_alerts.retain(|_, alert| { ... });
    self.noticed_alerts.retain(|a| { ... });
}
```

2. **In `AlertRelayer::received`**, add a `notice_until` freshness check mirroring the RPC path, rejecting alerts whose `notice_until` is already in the past.

3. Consider replacing the bounded `LruCache` for `cancel_filter` with an unbounded set or a time-bounded set to prevent eviction-based replay of cancelled alerts.

### Proof of Concept

```
// Setup: capture raw bytes of a valid alert with id=42, notice_until=T (future)
// Wait until T passes and a new peer connects to the target node (triggering clear_expired_alerts)
// Connect to target node as a P2P peer using the Alert protocol
// Send the original captured bytes verbatim
// Observe: target node re-accepts the alert, fires notify_network_alert, and re-broadcasts to peers
// Repeat indefinitely — no new signatures required
```

The `test_clear_expired_alerts` unit test confirms that after `clear_expired_alerts`, `has_received(1)` returns `false`, proving the replay window opens: [7](#0-6)

### Citations

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

**File:** util/network-alert/src/tests/test_notifier.rs (L101-104)
```rust
    notifier.clear_expired_alerts(after_expired_time);
    assert!(!notifier.has_received(1));
    assert_eq!(notifier.received_alerts().len(), 0);
    assert_eq!(notifier.noticed_alerts().len(), 0);
```
