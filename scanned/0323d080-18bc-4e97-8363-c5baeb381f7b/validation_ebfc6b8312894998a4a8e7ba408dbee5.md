### Title
Missing Chain-Specific Domain Separator and Expiry Check in Network Alert Signature Verification — (`util/network-alert/src/verifier.rs`, `util/network-alert/src/alert_relayer.rs`)

---

### Summary

The CKB network alert system signs `RawAlert` messages using a multi-sig scheme, but `calc_alert_hash()` hashes only the raw `RawAlert` bytes with no chain-specific identifier (genesis hash or chain spec hash). This makes valid alert signatures replayable across any CKB-based network that shares the same signing keys. Additionally, the P2P alert relayer (`alert_relayer.rs`) accepts and re-broadcasts incoming alerts without checking the `notice_until` expiry field, allowing any peer to replay expired alerts network-wide. Both flaws are direct analogs to the external report's user-supplied `domainSeparator` and missing deadline check.

---

### Finding Description

**Root Cause 1 — Missing chain-specific domain separator in alert hash**

`RawAlert` is defined in the Molecule schema as:

```
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
}
``` [1](#0-0) 

`calc_alert_hash()` simply hashes the raw bytes of `RawAlert` with no chain identifier mixed in:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
``` [2](#0-1) 

`Verifier::verify_signatures()` derives the message to verify directly from `alert.calc_alert_hash()` — no genesis hash, no chain spec hash, no network name is mixed into the signed digest:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [3](#0-2) 

Any CKB-based network (fork, testnet, staging) that uses the same alert signing keys will accept a signature produced for a different network, because the signed digest is identical across chains.

---

**Root Cause 2 — Missing `notice_until` expiry check in the P2P relayer**

The RPC `send_alert` handler does check expiry before accepting an alert:

```rust
let notice_until: u64 = alert.raw().notice_until().into();
if notice_until < now_ms {
    return Err(RPCError::invalid_params(...));
}
``` [4](#0-3) 

However, the P2P `AlertRelayer::received()` handler — the path used when an alert arrives from a peer — performs **no such check**. It only verifies the multi-sig and deduplicates by alert ID:

```rust
// verify
if let Err(err) = self.verifier.verify_signatures(&alert) {
    ...
    return;
}
// mark sender as known
self.mark_as_known(peer_index, alert_id);
// broadcast message
...
self.notifier.lock().add(&alert);
``` [5](#0-4) 

`Notifier::add()` also does not check `notice_until` when inserting the alert; it only calls `clear_expired_alerts()` on a separate timer:

```rust
pub fn add(&mut self, alert: &Alert) {
    ...
    self.received_alerts.insert(alert_id, alert.clone());
    ...
    self.notify_controller.notify_network_alert(alert.clone());
    self.noticed_alerts.push(alert.clone());
``` [6](#0-5) 

---

### Impact Explanation

**Cross-network alert replay (Root Cause 1):** Any CKB-based network (testnet, a fork, a staging environment) that shares the same hardcoded alert public keys will accept a signature produced for a different network. An attacker who obtains a historically valid alert from mainnet can replay it on testnet (or vice versa), causing all nodes on the target network to display the alert to operators. Since the signing keys are hardcoded in the source and shared across deployments by default, this is a realistic cross-network replay path.

**Expired alert replay via P2P (Root Cause 2):** Any unprivileged P2P peer can send a previously valid but now-expired alert directly over the Alert protocol. Because the P2P relayer skips the `notice_until` check, the receiving node accepts the alert, adds it to its notifier, and re-broadcasts it to all connected peers. This propagates the expired alert network-wide, causing every node to display it to operators. The `notice_until` field is part of the signed `RawAlert`, so the attacker cannot forge a new expiry — but they can replay any historically valid alert indefinitely.

---

### Likelihood Explanation

The P2P expired-alert replay requires only that an attacker:
1. Collect any previously broadcast, now-expired alert (trivially available from historical network data or public archives).
2. Connect to any CKB node as a peer (permissionless P2P network).
3. Send the alert bytes over the Alert protocol.

No privileged access, no key material, and no cryptographic break is required. The cross-network replay requires the same alert keys to be configured on multiple networks, which is the default for any CKB fork or testnet that does not explicitly rotate the keys.

---

### Recommendation

1. **Include a chain-specific domain separator in the alert hash.** Mix the genesis hash (available via `Consensus::genesis_hash()`) into the data hashed by `calc_alert_hash()`, so a signature produced for one network is cryptographically invalid on any other.

2. **Add a `notice_until` expiry check in `AlertRelayer::received()`**, mirroring the check already present in the RPC handler:
   ```rust
   let notice_until: u64 = alert.raw().notice_until().into();
   if notice_until < ckb_systemtime::unix_time_as_millis() {
       // ban or silently drop
       return;
   }
   ```

3. **Add the same expiry check in `Notifier::add()`** as a defense-in-depth measure, so that even if an expired alert reaches the notifier it is not displayed.

---

### Proof of Concept

**Expired alert replay via P2P:**

1. Capture any historically valid CKB alert (e.g., alert `id=20230001` from public records, with `notice_until=1681574400000` — April 2023).
2. Reconstruct the `Alert` molecule bytes (raw alert + original signatures).
3. Connect to a live CKB node as a P2P peer.
4. Send the alert bytes over the `Alert` protocol (protocol ID from `SupportProtocols::Alert`).
5. The node's `AlertRelayer::received()` parses the alert, calls `verify_signatures()` (which passes — the signatures are valid), skips any expiry check, calls `notifier.add()`, and re-broadcasts to all peers.
6. Every connected node displays the expired alert to its operator.

**Cross-network replay:**

1. Take a valid mainnet alert with valid mainnet signatures.
2. Submit it to a testnet node (or any CKB fork) that uses the same alert public keys.
3. `Verifier::verify_signatures()` computes `calc_alert_hash()` over the identical `RawAlert` bytes — the digest is the same on both networks — and the signature verifies successfully.
4. The testnet node accepts and propagates the mainnet alert.

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

**File:** util/gen-types/src/extension/calc_hash.rs (L292-300)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(RawAlert, calc_alert_hash);
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

**File:** util/network-alert/src/alert_relayer.rs (L150-179)
```rust
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

**File:** util/network-alert/src/notifier.rs (L92-122)
```rust
    /// Add an alert
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
