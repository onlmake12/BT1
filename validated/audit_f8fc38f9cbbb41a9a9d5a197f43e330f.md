### Title
Network Alert Signatures Lack Chain/Network Binding, Enabling Cross-Network Replay — (`util/network-alert/src/verifier.rs`)

---

### Summary

The `Verifier::verify_signatures` function in the CKB network-alert subsystem computes the signed message as `blake2b_256(raw_alert_bytes)`, where `RawAlert` contains no network or chain identifier. Because the same four Nervos Foundation operator keys are hard-coded as defaults for every CKB network (mainnet, testnet, devnet), a valid signed alert from one network is cryptographically indistinguishable from a valid alert on any other network. An unprivileged attacker who observes a legitimately signed testnet alert can replay it verbatim on mainnet — passing full signature verification and triggering a network-wide broadcast of a false security warning.

---

### Finding Description

**Schema — no network field in `RawAlert`:**

The `RawAlert` molecule type is defined as:

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

There is no `chain_id`, `genesis_hash`, or any other network-scoped field.

**Hash computation — pure hash of raw bytes, no domain separation:**

`calc_alert_hash()` delegates to `CalcHash::calc_hash()`, which is:

```rust
fn calc_hash(&self) -> packed::Byte32 {
    blake2b_256(self.as_slice()).into()
}
``` [2](#0-1) 

The `RawAlertReader::calc_alert_hash` simply calls this:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()
}
``` [3](#0-2) 

No network context is mixed into the hash.

**Verifier — message is the bare alert hash:**

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [4](#0-3) 

**Same operator keys hard-coded for all networks:**

```toml
# need 2 signatures to send alert
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [5](#0-4) 

This config is loaded as the `Default` for `NetworkAlertConfig` and applied to every CKB network instance:

```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
}
``` [6](#0-5) 

---

### Impact Explanation

A valid signed alert broadcast on testnet (or any other CKB-based network sharing the same operator keys) carries signatures over `blake2b_256(raw_alert_bytes)`. Because `raw_alert_bytes` contains no network discriminator, those exact signatures are equally valid when verified by a mainnet node's `Verifier`. An attacker who replays the alert:

1. Passes `verify_signatures` on mainnet without modification.
2. Causes `Notifier::add` to store the alert and call `notify_controller.notify_network_alert`, surfacing a false security warning to every mainnet operator.
3. Triggers `AlertRelayer` to re-broadcast the alert to all connected peers, propagating the false alarm across the entire mainnet P2P network. [7](#0-6) [8](#0-7) 

The alert system is explicitly reserved for critical security announcements. A false mainnet-wide alert could cause mass unnecessary node upgrades, operational panic, or loss of trust in the alert mechanism.

---

### Likelihood Explanation

- The same four operator keys are the compile-time default for all CKB networks; no configuration change is needed for the attack to work.
- Testnet alerts are broadcast publicly over P2P and are trivially observable by any peer.
- The attacker needs zero privileges: the `send_alert` RPC is open to any local or remote RPC caller, and the P2P `Alert` protocol accepts messages from any connected peer.
- The `alert_relayer.rs` P2P handler accepts inbound alert messages from any peer index and only checks signature validity — not network origin. [9](#0-8) [10](#0-9) 

---

### Recommendation

Include a network-scoped domain separator in the alert hash. The genesis block hash (already available in `Consensus`) is the canonical CKB network identifier and should be mixed into the signed message:

```rust
// Pseudocode
let message = blake2b_256([genesis_hash, raw_alert.as_slice()].concat());
```

Alternatively, add a `network_id: Bytes` field to `RawAlert` that signers must populate with the target network's genesis hash before signing. Either approach ensures a signature produced for testnet is cryptographically invalid on mainnet.

---

### Proof of Concept

1. On testnet, observe (or obtain) a legitimately signed `Alert` message — e.g., from the P2P wire or from a public `send_alert` RPC call. The alert carries two valid Foundation signatures over `blake2b_256(raw_alert_bytes)`.

2. Submit the identical alert bytes to a mainnet node's `send_alert` RPC:

```json
{
  "jsonrpc": "2.0",
  "method": "send_alert",
  "params": [{
    "id": "0x<testnet_alert_id>",
    "cancel": "0x0",
    "priority": "0x1",
    "message": "<testnet alert message>",
    "notice_until": "0x<future_timestamp>",
    "signatures": [
      "0x<foundation_sig_1_from_testnet>",
      "0x<foundation_sig_2_from_testnet>"
    ]
  }],
  "id": 1
}
```

3. `Verifier::verify_signatures` computes `blake2b_256(raw_alert_bytes)` — identical to the testnet hash since `RawAlert` contains no network field — and calls `verify_m_of_n` against the same hard-coded Foundation pubkeys. Verification succeeds.

4. The mainnet node stores the alert, notifies all local subscribers, and re-broadcasts it to all connected mainnet peers via `AlertRelayer`, propagating the false alarm network-wide.

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

**File:** util/gen-types/src/extension/calc_hash.rs (L19-21)
```rust
    fn calc_hash(&self) -> packed::Byte32 {
        blake2b_256(self.as_slice()).into()
    }
```

**File:** util/gen-types/src/extension/calc_hash.rs (L296-298)
```rust
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
```

**File:** util/network-alert/src/verifier.rs (L33-62)
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

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
```

**File:** util/network-alert/src/notifier.rs (L93-116)
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
```

**File:** util/network-alert/src/alert_relayer.rs (L97-162)
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

**File:** util/network-alert/src/alert_relayer.rs (L163-178)
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
```

**File:** rpc/src/module/alert.rs (L102-131)
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

        let result = self.verifier.verify_signatures(&alert);

        match result {
            Ok(()) => {
                // set self node notifier
                self.notifier.lock().add(&alert);

                self.network_controller.broadcast_with_handle(
                    SupportProtocols::Alert.protocol_id(),
                    alert.as_bytes(),
                    &self.handle,
                );
                Ok(())
            }
            Err(e) => Err(RPCError::custom_with_error(
                RPCError::AlertFailedToVerifySignatures,
                e,
            )),
        }
    }
```
