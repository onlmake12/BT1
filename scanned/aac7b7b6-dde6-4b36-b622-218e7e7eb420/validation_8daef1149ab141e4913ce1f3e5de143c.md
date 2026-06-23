### Title
Zero `signatures_threshold` Bypasses Alert Signature Verification Entirely - (`File: util/multisig/src/secp256k1.rs`, `util/app-config/src/configs/network_alert.rs`, `util/network-alert/src/verifier.rs`)

---

### Summary

`verify_m_of_n()` in `util/multisig/src/secp256k1.rs` contains no guard against `m_threshold = 0`. When `NetworkAlertConfig.signatures_threshold` is set to `0` in `ckb.toml`, `Verifier::verify_signatures()` trivially returns `Ok(())` for any alert with an empty signatures list. This allows any RPC caller or P2P peer to inject and broadcast arbitrary unsigned network alerts to the entire CKB P2P network without possessing any of the Nervos Foundation signing keys.

---

### Finding Description

**Root cause 1 — `verify_m_of_n` has no zero-threshold guard**

`util/multisig/src/secp256k1.rs` lines 20–54:

```rust
pub fn verify_m_of_n<S>(
    message: &Message,
    m_threshold: usize,   // ← no check that this is > 0
    sigs: &[Signature],
    pks: &HashSet<Pubkey, S>,
) -> Result<(), Error> {
    if sigs.len() > pks.len() {          // 0 > N → false, passes
        return Err(ErrorKind::SigCountOverflow.into());
    }
    if m_threshold > sigs.len() {        // 0 > 0 → false, passes
        return Err(ErrorKind::SigNotEnough.into());
    }
    // verified_sig_count = 0 (no sigs to iterate)
    if verified_sig_count < m_threshold { // 0 < 0 → false, passes
        return Err(ErrorKind::Threshold { ... }.into());
    }
    Ok(())   // ← returns success with zero signatures
}
```

With `m_threshold = 0` and `sigs = []`, all three guards evaluate to `false` and the function returns `Ok(())`. [1](#0-0) [2](#0-1) 

**Root cause 2 — `NetworkAlertConfig` accepts `signatures_threshold = 0` without validation**

`util/app-config/src/configs/network_alert.rs`:

```rust
pub struct Config {
    pub signatures_threshold: usize,   // ← no minimum-value constraint
    pub public_keys: Vec<JsonBytes>,
}
```

No validation is performed in `Default::default()`, in `Verifier::new()`, or anywhere in the deserialization path to reject a zero threshold. [3](#0-2) 

**Root cause 3 — `Verifier::new()` does not validate the threshold**

`util/network-alert/src/verifier.rs` lines 22–29: `Verifier::new()` parses public keys but never asserts `config.signatures_threshold > 0`. [4](#0-3) 

**Propagation — `verify_signatures` passes the raw threshold directly**

`util/network-alert/src/verifier.rs` lines 56–62:

```rust
verify_m_of_n(
    &message,
    self.config.signatures_threshold,   // ← 0 when misconfigured
    &signatures,
    &self.pubkeys,
)
``` [5](#0-4) 

**Two reachable entry paths once `signatures_threshold = 0` is set:**

1. **RPC path** — `rpc/src/module/alert.rs` `send_alert()` calls `self.verifier.verify_signatures(&alert)` and broadcasts on success. Any RPC caller submitting an alert with `"signatures": []` passes verification. [6](#0-5) 

2. **P2P path** — `util/network-alert/src/alert_relayer.rs` `received()` calls `self.verifier.verify_signatures(&alert)` on every inbound P2P alert message. Any connected peer sending an alert with an empty signatures field passes verification and the alert is re-broadcast to all connected peers. [7](#0-6) 

---

### Impact Explanation

Network alerts are broadcast to every CKB node on the P2P network and displayed to node operators as critical system messages. If `signatures_threshold = 0` is configured, the entire multi-signature protection of the alert system is nullified. Any RPC caller or P2P peer can inject arbitrary alert content (fake upgrade warnings, false emergency messages) that propagates to all nodes without any of the four Nervos Foundation signing keys. This undermines the integrity of the only out-of-band emergency communication channel in CKB. [8](#0-7) 

---

### Likelihood Explanation

Likelihood is **low but non-negligible**. The default `alert_signature.toml` ships with `signatures_threshold = 2`, so a standard deployment is safe. However:

- The `NetworkAlertConfig` struct is fully user-overridable via `ckb.toml` with no runtime validation.
- A node operator who sets `signatures_threshold = 0` (e.g., for local testing and forgets to revert, or through a misconfigured deployment script) silently disables all alert authentication.
- Once one node accepts and re-broadcasts an unsigned alert, the alert propagates to all peers regardless of their own threshold setting, because the alert is already in the network. [9](#0-8) [10](#0-9) 

---

### Recommendation

1. **Guard `verify_m_of_n` against zero threshold** — add `if m_threshold == 0 { return Err(ErrorKind::ZeroThreshold.into()); }` at the top of the function in `util/multisig/src/secp256k1.rs`.

2. **Validate `signatures_threshold` at construction time** — in `Verifier::new()` (`util/network-alert/src/verifier.rs`), assert `config.signatures_threshold > 0` and panic or return an error if it is zero, analogous to how `expect("builtin pubkeys")` already enforces valid public keys.

3. **Add a serde validation attribute or a post-deserialization check** on `NetworkAlertConfig` to reject `signatures_threshold = 0` at config-load time, preventing a misconfigured node from starting.

---

### Proof of Concept

With `ckb.toml` containing:
```toml
[alert_signature]
signatures_threshold = 0
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  ...
]
```

An unprivileged RPC caller sends:
```json
{
  "jsonrpc": "2.0",
  "method": "send_alert",
  "params": [{
    "id": "0x1",
    "cancel": "0x0",
    "priority": "0x1",
    "message": "FAKE: CKB network is under attack, send funds to safe address X",
    "notice_until": "0x99999999999",
    "signatures": []
  }],
  "id": 1
}
```

Execution trace:
- `send_alert` → `verifier.verify_signatures(&alert)`
- `verify_m_of_n(msg, 0, [], pubkeys)` → all guards pass → `Ok(())`
- Alert is stored in `notifier` and broadcast via `network_controller.broadcast_with_handle(SupportProtocols::Alert, ...)`
- Every connected peer's `AlertRelayer::received()` calls `verify_m_of_n(msg, 0, [], pubkeys)` → `Ok(())` → re-broadcasts to their peers
- Alert propagates to the entire CKB P2P network with no valid signature [11](#0-10) [12](#0-11) [13](#0-12)

### Citations

**File:** util/multisig/src/secp256k1.rs (L11-25)
```rust
pub fn verify_m_of_n<S>(
    message: &Message,
    m_threshold: usize,
    sigs: &[Signature],
    pks: &HashSet<Pubkey, S>,
) -> Result<(), Error>
where
    S: BuildHasher,
{
    if sigs.len() > pks.len() {
        return Err(ErrorKind::SigCountOverflow.into());
    }
    if m_threshold > sigs.len() {
        return Err(ErrorKind::SigNotEnough.into());
    }
```

**File:** util/multisig/src/secp256k1.rs (L47-54)
```rust
    if verified_sig_count < m_threshold {
        return Err(ErrorKind::Threshold {
            pass_sigs: verified_sig_count,
            threshold: m_threshold,
        }
        .into());
    }
    Ok(())
```

**File:** util/app-config/src/configs/network_alert.rs (L7-12)
```rust
pub struct Config {
    /// The minimum number of required signatures to send a network alert.
    pub signatures_threshold: usize,
    /// The public keys of all the network alert signers.
    pub public_keys: Vec<JsonBytes>,
}
```

**File:** util/network-alert/src/verifier.rs (L22-29)
```rust
    pub fn new(config: NetworkAlertConfig) -> Self {
        let pubkeys = config
            .public_keys
            .iter()
            .map(|raw| Pubkey::from_slice(raw.as_bytes()))
            .collect::<Result<HashSet<Pubkey>, _>>()
            .expect("builtin pubkeys");
        Verifier { config, pubkeys }
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

**File:** util/network-alert/src/alert_relayer.rs (L98-179)
```rust
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

**File:** util/network-alert/src/lib.rs (L1-8)
```rust
//! Network Alert
//! See <https://en.bitcoin.it/wiki/Alert_system> to learn the history of Bitcoin alert system.
//! We implement the alert system in CKB for urgent situation,
//! In CKB early stage we may meet the same crisis bugs that Bitcoin meet,
//! in urgent case, CKB core team can send an alert message across CKB P2P network,
//! the client will show the alert message, the other behaviors of CKB node will not change.
//!
//! Network Alert will be removed soon once the CKB network is considered mature.
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

**File:** util/launcher/src/lib.rs (L478-483)
```rust
        let alert_signature_config = self.args.config.alert_signature.clone().unwrap_or_default();
        let alert_relayer = AlertRelayer::new(
            self.version.short(),
            shared.notify_controller().clone(),
            alert_signature_config,
        );
```
