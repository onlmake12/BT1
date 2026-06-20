### Title
Network Alert Hash Missing Chain-Specific Domain Separator Enables Cross-Network Signature Replay - (File: `util/gen-types/src/extension/calc_hash.rs`, `util/network-alert/src/verifier.rs`)

---

### Summary

`calc_alert_hash()` produces a plain `blake2b(raw_alert_bytes)` with no chain ID, genesis hash, or network identifier. The same four alert-signing public keys are hard-coded for all CKB networks. An unprivileged P2P peer can take a valid mainnet alert (a public P2P message) and replay it verbatim on testnet nodes, where it passes signature verification and is accepted, stored, and re-broadcast to all peers.

---

### Finding Description

The `RawAlertReader::calc_alert_hash()` function delegates directly to `calc_hash()`, which is a plain `blake2b_256(self.as_slice())` over the raw alert bytes: [1](#0-0) 

The `CalcHash` trait implementation confirms there is no domain prefix, chain ID, or genesis hash mixed into the digest: [2](#0-1) 

The `Verifier::verify_signatures()` function constructs the signing message directly from this hash and checks it against the hard-coded public key set: [3](#0-2) 

The default public key set is identical across all networks (mainnet, testnet, devnet): [4](#0-3) 

Because the signed message is `blake2b(raw_alert_bytes)` with no network context, a signature valid on mainnet is cryptographically identical to a signature valid on testnet for the same alert content.

The P2P `AlertRelayer::received()` handler accepts any alert that passes signature verification, stores it, and re-broadcasts it to all connected peers: [5](#0-4) 

Additionally, the `connected()` handler automatically pushes all stored alerts to every newly connecting peer: [6](#0-5) 

This makes the injection self-propagating: once one testnet node accepts a replayed mainnet alert, it re-broadcasts it to all its peers.

The `Notifier::add()` function processes the `cancel` field unconditionally: [7](#0-6) 

A mainnet alert with `cancel > 0` replayed on testnet will silently remove the corresponding testnet alert from all nodes that accept it.

---

### Impact Explanation

1. **Alert injection**: An unprivileged P2P peer can cause all testnet nodes to display a mainnet alert message. The alert system is the designated mechanism for communicating critical security bugs; injecting foreign messages undermines its integrity.
2. **Alert cancellation**: If a mainnet alert carries `cancel = X`, replaying it on testnet removes testnet alert `X` from every node that processes it. A legitimate testnet security warning can be silently suppressed network-wide.
3. **Self-propagating**: Once accepted by one node, the replayed alert is forwarded to all peers via the `connected()` push, requiring only a single injection point.

---

### Likelihood Explanation

Mainnet alerts are broadcast over the public P2P network and are trivially observable by any participant. No key material, privileged access, or brute force is required. Any unprivileged P2P peer who has observed a mainnet alert can immediately replay it on testnet by connecting to testnet nodes and sending the same raw bytes via the Alert protocol. The `notice_until` expiry check exists only in the `send_alert` RPC path; the P2P `received()` handler performs no expiry check at receive time, so even expired mainnet alerts can be injected via P2P.

---

### Recommendation

Include a network-specific domain separator in the alert signing message. The genesis hash already serves as a unique per-network identifier in CKB (used in `identify_name()`). The alert hash should be computed as:

```
blake2b(genesis_hash || raw_alert_bytes)
```

or equivalently, a tagged hash:

```
blake2b("ckb-alert" || genesis_hash || raw_alert_bytes)
```

This ensures that a signature produced for a mainnet alert is cryptographically invalid on testnet and vice versa, directly mirroring the fix recommended in the ERC-7739 report (binding the signed message to the specific network context rather than using a shared, context-free hash).

---

### Proof of Concept

1. Run a CKB mainnet node and a CKB testnet node.
2. Observe a valid alert broadcast on mainnet (e.g., alert `20230001` with its two known signatures from `generate_alert_signature.rs`).
3. Connect a custom P2P client to a testnet node using the Alert protocol (`SupportProtocols::Alert`).
4. Send the raw mainnet alert bytes verbatim.
5. The testnet `AlertRelayer::received()` calls `verifier.verify_signatures(&alert)`. Since `calc_alert_hash()` produces the same digest for the same raw bytes and the public keys are identical, verification succeeds.
6. The testnet node stores the alert and re-broadcasts it to all connected testnet peers.
7. If the mainnet alert had `cancel > 0`, the corresponding testnet alert is removed from all nodes that process the replayed message.

### Citations

**File:** util/gen-types/src/extension/calc_hash.rs (L15-22)
```rust
impl<'r, R> CalcHash for R
where
    R: Reader<'r>,
{
    fn calc_hash(&self) -> packed::Byte32 {
        blake2b_256(self.as_slice()).into()
    }
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

**File:** util/app-config/src/configs/alert_signature.toml (L1-8)
```text
# need 2 signatures to send alert
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
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

**File:** util/network-alert/src/alert_relayer.rs (L144-178)
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
