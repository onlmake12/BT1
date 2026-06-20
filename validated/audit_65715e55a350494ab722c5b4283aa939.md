### Title
Hardcoded Alert Public Keys Embedded at Compile Time Cannot Be Rotated Without Full Binary Redeployment - (File: `util/app-config/src/configs/network_alert.rs`)

---

### Summary

CKB's network alert system embeds its 4 secp256k1 signer public keys directly into the compiled binary via `include_bytes!` at compile time. If any of these keys are compromised, there is no mechanism to revoke or rotate them without releasing and deploying a new CKB binary across the entire network. During the exposure window, an attacker holding 2 of the 4 private keys can broadcast arbitrary alert messages to every node via the P2P network.

---

### Finding Description

`util/app-config/src/configs/network_alert.rs` implements `Default` for `NetworkAlertConfig` by calling `include_bytes!("./alert_signature.toml")`, which embeds the TOML file's contents directly into the binary at compile time:

```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
}
``` [1](#0-0) 

The embedded file `util/app-config/src/configs/alert_signature.toml` contains 4 hardcoded secp256k1 compressed public keys with a 2-of-4 threshold:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [2](#0-1) 

These keys are loaded into `Verifier` in `util/network-alert/src/verifier.rs`, which is the sole authority for accepting or rejecting all alert messages across the P2P network: [3](#0-2) 

The RPC documentation itself acknowledges the design: *"The alerts must be signed by 2-of-4 signatures, where the public keys are hard-coded in the source code and belong to early CKB developers."* [4](#0-3) 

Unlike a runtime-configurable file, `include_bytes!` resolves at compile time. The keys are frozen into the binary and cannot be changed without recompiling and redeploying CKB to every node on the network.

---

### Impact Explanation

If 2 or more of the 4 private keys are compromised, an attacker can:

1. Craft arbitrary `packed::Alert` messages with valid 2-of-4 signatures.
2. Submit them via the `send_alert` RPC endpoint (reachable by any local RPC caller) or inject them directly into the P2P network via the `AlertRelayer` protocol handler.
3. Every node that receives the alert will pass `verify_signatures`, store it, and relay it to all connected peers — achieving network-wide propagation. [5](#0-4) 

Node operators receiving a fake but cryptographically valid alert may shut down nodes, upgrade to attacker-supplied software, or take other harmful actions. Because the keys are baked into the binary, there is no on-chain or config-level revocation mechanism. The only remediation is a coordinated emergency release and network-wide upgrade, during which the attacker retains full ability to broadcast alerts.

**Impact: 3** — Network-wide fake alert broadcast; potential for operator-level harm and disruption.

---

### Likelihood Explanation

**Likelihood: 2** — Requires compromise of at least 2 of 4 private keys held by early CKB developers. The keys have been static since the alert system was introduced (v0.14.0, 2019), increasing long-term exposure risk. The threshold is only 2-of-4, meaning a single developer's key compromise plus one more is sufficient.

---

### Recommendation

Load the alert public keys from a runtime-configurable file path (e.g., `ckb.toml`) rather than embedding them via `include_bytes!` at compile time. This would allow key rotation without requiring a full binary redeployment. The `NetworkAlertConfig` struct already supports this pattern — the `Default` implementation is the only place that hard-pins the keys into the binary. [1](#0-0) 

---

### Proof of Concept

**Step 1 — Root cause:** `include_bytes!` in `Default` bakes keys into binary at compile time. [6](#0-5) 

**Step 2 — Hardcoded keys:** 4 static public keys, 2-of-4 threshold. [2](#0-1) 

**Step 3 — Verification gate:** All P2P-received alerts pass through `verify_signatures` using only these keys. [7](#0-6) 

**Step 4 — P2P entry path:** Any peer can deliver an alert message to `AlertRelayer::received`; if signatures verify, it is stored and rebroadcast to all connected peers. [8](#0-7) 

**Step 5 — RPC entry path:** Any RPC caller can submit an alert via `send_alert`. [9](#0-8)

### Citations

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
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

**File:** rpc/src/module/alert.rs (L17-19)
```rust
///
/// The alerts must be signed by 2-of-4 signatures, where the public keys are hard-coded in the source code
/// and belong to early CKB developers.
```

**File:** rpc/src/module/alert.rs (L72-73)
```rust
    #[rpc(name = "send_alert")]
    fn send_alert(&self, alert: Alert) -> Result<()>;
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
