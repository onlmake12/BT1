### Title
Network Alert Signature Replay Across CKB Networks (Mainnet/Testnet) Due to Missing Chain Identifier in Signed Message - (`util/network-alert/src/verifier.rs`)

### Summary

The `RawAlert` molecule schema and its `calc_alert_hash()` function do not include any network/chain identifier in the signed message. Because CKB mainnet and testnet (Aggron) ship the same binary with the same four hard-coded alert public keys, a valid alert signature produced for one network is cryptographically valid on the other. Any unprivileged P2P peer can capture a signed alert from one network and replay it verbatim on the other.

### Finding Description

The `RawAlert` struct contains only: `notice_until`, `id`, `cancel`, `priority`, `message`, `min_version`, `max_version`. [1](#0-0) 

The alert hash that signers sign is computed as a plain blake2b hash of the raw `RawAlert` bytes — no network name, no genesis hash, no chain ID is mixed in. [2](#0-1) 

The `Verifier::verify_signatures()` derives the message to verify solely from `alert.calc_alert_hash()`: [3](#0-2) 

The default `NetworkAlertConfig` is compiled directly from a single `alert_signature.toml` file that is embedded in the binary at build time. Both mainnet and testnet nodes running the same release binary therefore share the **identical** set of four hard-coded public keys: [4](#0-3) [5](#0-4) 

The P2P `received` handler in `AlertRelayer` verifies signatures and relays the alert without any check on which network the alert was intended for, and without checking `notice_until` expiry: [6](#0-5) 

The `notice_until` expiry check exists **only** in the RPC `send_alert` handler, not in the P2P relay path: [7](#0-6) 

### Impact Explanation

An attacker who is a P2P peer on CKB testnet can:

1. Observe a legitimately signed mainnet alert propagating on mainnet.
2. Connect to testnet nodes and inject the same raw alert bytes.
3. Testnet nodes accept it: same keys, same hash, no chain binding — `verify_signatures` passes.
4. The alert propagates across all testnet nodes and is displayed to operators.

The reverse direction (testnet alert → mainnet) is equally possible. Because the `notice_until` expiry is not checked in the P2P path, even an alert whose `notice_until` has already passed can be re-injected via P2P after the notifier clears it (once `has_received` returns false for the cleared ID).

Impact: node operators on the wrong network receive misleading critical-upgrade warnings, potentially triggering unnecessary emergency upgrades, node shutdowns, or panic. The alert system is the primary out-of-band emergency communication channel for the Nervos Foundation; its integrity is security-critical.

### Likelihood Explanation

- **Same binary, same keys**: Every default CKB node on both mainnet and testnet uses the identical four public keys embedded at compile time. No special setup is required.
- **Attacker capability**: Any node that can connect to the P2P network (no privilege required) can relay arbitrary alert bytes. The attacker does not need any signing key — only a previously broadcast alert from the other network.
- **Concrete trigger**: CKB has issued real alerts (e.g., alert `20230001` for v0.105.x bugs). Those signed bytes are publicly observable on-chain/in-network and can be replayed on testnet at any time. [8](#0-7) 

### Recommendation

Include a network/chain domain separator in the signed alert message. The natural choice is the genesis block hash (already available in `Consensus`), mixed into `calc_alert_hash()` before signing. Concretely, change `RawAlert` to include a `chain_id: Byte32` field (set to the genesis hash), or compute the alert hash as `blake2b(genesis_hash || raw_alert_bytes)`. Additionally, add a `notice_until` expiry check in `AlertRelayer::received()` to close the expired-alert replay path via P2P.

### Proof of Concept

The following demonstrates the cross-network replay. The same signed alert bytes accepted by a mainnet verifier (using default keys) are accepted by a testnet verifier (also using default keys), because both use `NetworkAlertConfig::default()` from the same binary:

```rust
// Both mainnet and testnet nodes use NetworkAlertConfig::default() → same 4 pubkeys
let mainnet_verifier = Verifier::new(NetworkAlertConfig::default());
let testnet_verifier = Verifier::new(NetworkAlertConfig::default());

// Alert signed by a Nervos Foundation key holder for mainnet
let raw_alert = packed::RawAlert::new_builder()
    .notice_until(1681574400000u64)
    .id(20230001u32)
    .message("CKB v0.105.* have bugs. Please upgrade.")
    .build();
// Signatures from alert_20230001 (publicly known):
let alert = packed::Alert::new_builder().raw(raw_alert).signatures(known_sigs).build();

// Passes on mainnet:
assert!(mainnet_verifier.verify_signatures(&alert).is_ok());
// Passes identically on testnet — no chain binding:
assert!(testnet_verifier.verify_signatures(&alert).is_ok());
// Attacker connects to testnet P2P and sends `alert.as_bytes()` — accepted and relayed.
```

The existing test `test_alert_20230001` already demonstrates that the real mainnet alert `20230001` verifies against `NetworkAlertConfig::default()`. [8](#0-7) [9](#0-8) [4](#0-3)

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

**File:** util/network-alert/src/alert_relayer.rs (L144-179)
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
    }
```

**File:** rpc/src/module/alert.rs (L102-111)
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

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L58-93)
```rust
#[test]
fn test_alert_20230001() {
    let config = NetworkAlertConfig::default();
    let verifier = Verifier::new(config);
    let raw_alert = packed::RawAlert::new_builder()
        // 3 months later
        .notice_until(1681574400000u64)
        .id(20230001u32)
        .cancel(0u32)
        .priority(20u32)
        .message("CKB v0.105.* have bugs. Please upgrade to the latest version.")
        .min_version(Some("0.105.0-pre"))
        .max_version(Some("0.105.1"))
        .build();

    let signatures = [
        "8dca283684ff3cd024bd6a67efb24617e90e31dc69ac809ac6ac5e243a57b7aa6711228dfbd8a5cc89a68d3065b685e5c56c70740e8d3487fd538dc914d0c97c00",
        "4554b37824e17ea02432507e372c869301a415bf718e0a5a33b6df75cd32fbab7cf8176ca8b079c28266ce1f33c3f61fbff19e27be2a85f5a14faa2b1b474e0a01"
    ].iter().map(|hex| {
        let mut buf = vec![0u8; hex.len() / 2];
        hex_decode(hex.as_bytes(), &mut buf).expect("valid hex");
        buf.into()
    }).fold(packed::BytesVec::new_builder(), |builder, item: packed::Bytes| {
        builder.push(item)
    }).build();
    let alert = packed::Alert::new_builder()
        .raw(raw_alert)
        .signatures(signatures)
        .build();
    let alert_json = Alert::from(alert.clone());
    println!(
        "Alert:\n{}",
        serde_json::to_string_pretty(&alert_json).unwrap()
    );
    assert!(verifier.verify_signatures(&alert).is_ok());
}
```
