### Title
Network Alert Signature Hash Missing Chain Identifier Allows Cross-Network Replay — (`util/network-alert/src/verifier.rs`)

### Summary

The `RawAlert` message hash used for multi-signature verification does not include any chain identifier (chain name, genesis hash, or network ID). Because the same four developer public keys are hard-coded as the default alert signers for all CKB networks (mainnet `ckb` and testnet `ckb_testnet`), a validly signed alert from one network passes signature verification on any other network. Any unprivileged peer or RPC caller can replay a legitimately signed mainnet alert on testnet (or vice versa), causing false critical alerts to propagate across the wrong network.

### Finding Description

The `RawAlert` molecule schema contains no chain-identifying field:

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

The alert hash is computed by hashing only the raw bytes of this struct — no chain name, genesis hash, or network identifier is mixed in:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
``` [2](#0-1) 

The verifier uses this hash directly as the signed message:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    // ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [3](#0-2) 

The default alert signer configuration is a single hard-coded `alert_signature.toml` file with four public keys and a threshold of 2:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  ...
]
``` [4](#0-3) 

`NetworkAlertConfig::default()` loads this same file for every network: [5](#0-4) 

At node startup, the launcher uses `unwrap_or_default()`, meaning both mainnet and testnet nodes use the identical key set unless explicitly overridden: [6](#0-5) 

The alert is accepted via the public `send_alert` RPC and also via the P2P `AlertRelayer`, both of which call `verify_signatures` with no chain context: [7](#0-6) [8](#0-7) 

### Impact Explanation

An attacker who observes a legitimately signed alert on mainnet (e.g., "CKB v0.X.* has a critical bug, upgrade immediately") can submit the identical `Alert` struct — with its original signatures — to a testnet node via `send_alert` RPC or by relaying it over P2P. Because the hash covers only the `RawAlert` content (no chain identifier), and the same developer public keys are configured on both networks, `verify_signatures` returns `Ok(())` on the testnet node. The alert is then stored and broadcast to all connected testnet peers. The reverse direction (testnet alert replayed on mainnet) is equally possible.

This undermines the integrity of the alert system, which is the sole mechanism for Nervos Foundation developers to communicate critical security information to node operators. False alerts on the wrong network can cause unnecessary panic, incorrect upgrade actions, or erode trust in future legitimate alerts.

### Likelihood Explanation

The attack requires no privileged access. The attacker only needs to:
1. Observe a valid signed alert on one network (alerts are broadcast publicly over P2P and visible via `get_blockchain_info` RPC).
2. Submit it to a node on the other network via the `send_alert` RPC (when the Alert module is enabled) or relay it via a P2P connection.

Both entry points are reachable by any unprivileged peer or RPC caller. The mainnet and testnet have been running simultaneously for years, and real alerts have been issued (e.g., alert ID `20230001`), providing concrete replay material.

### Recommendation

Include a chain domain separator in the signed message. The simplest fix is to mix the chain's genesis hash or consensus ID into the alert hash computation. For example, `calc_alert_hash` could be changed to hash `(genesis_hash || raw_alert_bytes)`, or a `chain_id` field could be added to `RawAlert`. The `Verifier` should be initialized with the chain's genesis hash and incorporate it into the message before calling `verify_m_of_n`.

### Proof of Concept

1. Run a mainnet node and a testnet node, both with the Alert RPC module enabled and default `alert_signature` config.
2. On the mainnet node, call `get_blockchain_info` — observe any active alert (e.g., alert `20230001` with its two known signatures).
3. Submit the identical `Alert` JSON (same `id`, `message`, `notice_until`, `signatures`) to the testnet node via `send_alert`.
4. The testnet node accepts it (signature verification passes because the hash is identical and the same keys are configured), stores it, and broadcasts it to all connected testnet peers.
5. Calling `get_blockchain_info` on any testnet peer now shows the mainnet alert as active.

The real alert `20230001` with its known signatures from `util/network-alert/src/tests/generate_alert_signature.rs` demonstrates that the hash is purely a function of the `RawAlert` content with no chain binding. [9](#0-8)

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

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
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

**File:** util/network-alert/src/alert_relayer.rs (L150-162)
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
