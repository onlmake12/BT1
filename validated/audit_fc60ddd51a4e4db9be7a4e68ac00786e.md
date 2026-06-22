### Title
Network Alert Signatures Lack Chain Identifier, Enabling Cross-Chain Replay - (`util/network-alert/src/verifier.rs`)

### Summary

The CKB network alert system signs and verifies alerts using a hash (`calc_alert_hash`) that is computed solely from the `RawAlert` payload bytes, with no chain-specific identifier (genesis hash, chain spec name, or network ID) included. Because the same four Nervos Foundation public keys are hardcoded for every network (mainnet, testnet, devnet, and any fork), a valid alert signed for one chain passes signature verification on any other chain. Any unprivileged P2P peer or RPC caller can capture a legitimate alert from one chain and replay it on another.

### Finding Description

`calc_alert_hash()` is implemented in `util/gen-types/src/extension/calc_hash.rs`:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
``` [1](#0-0) 

This hashes only the raw molecule-encoded bytes of `RawAlert`, which contains `id`, `cancel`, `min_version`, `max_version`, `priority`, `notice_until`, and `message` — no genesis hash, no chain spec name, no network ID.

`Verifier::verify_signatures()` in `util/network-alert/src/verifier.rs` derives the signing message directly from this chain-agnostic hash:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [2](#0-1) 

The public keys used for verification are loaded from a single hardcoded config file `util/app-config/src/configs/alert_signature.toml`, which is the same for all networks via `NetworkAlertConfig::default()`:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  ...
]
``` [3](#0-2) [4](#0-3) 

The `AlertRelayer` in `util/network-alert/src/alert_relayer.rs` accepts alert messages from any P2P peer and passes them through `verify_signatures()`. The `send_alert` RPC endpoint in `rpc/src/module/alert.rs` also accepts alerts from any RPC caller: [5](#0-4) [6](#0-5) 

Note that while the P2P identify protocol does include the genesis hash in the network name (`identify_name()` in `spec/src/consensus.rs`), this only governs P2P session establishment — it does not protect the alert signature domain: [7](#0-6) 

### Impact Explanation

An alert legitimately signed by the Nervos Foundation for mainnet (e.g., "CKB v0.105.x has a critical bug, upgrade immediately") can be submitted verbatim to testnet or any forked chain. The receiving nodes will accept and broadcast it because the signature is valid — the same keys are used and the hash contains no chain discriminator. Node operators on the target chain will see a misleading alert, potentially causing unnecessary panic, incorrect software upgrades, or suppression of a real alert (if the replayed alert's `id` collides with or cancels a legitimate one on that chain). In a hard-fork scenario, this is directly analogous to the GMX oracle replay: an alert from the original chain is valid on the fork, and vice versa.

### Likelihood Explanation

The `send_alert` RPC requires no authentication beyond the alert's own multi-signature, which is already satisfied by the replayed payload. Any RPC caller or P2P peer that has observed a legitimate alert on one chain can immediately replay it on another. No key material needs to be compromised. The only prerequisite is that the Nervos Foundation has previously issued at least one alert on any CKB-compatible network — a condition that has already been met (alert `20230001` exists in the test suite with real production signatures).

### Recommendation

Include a chain-specific domain separator in the signed message. The genesis hash is already computed and available via `Consensus::genesis_hash`. The `calc_alert_hash` computation should commit to it:

```rust
// Pseudocode
let message = blake2b(genesis_hash || raw_alert.as_slice());
```

Alternatively, pass the genesis hash into `Verifier` at construction time and mix it into the message before calling `verify_m_of_n`. This mirrors the fix applied in the GMX report: include the chain identifier in the signed data and look it up dynamically rather than relying on a static, chain-agnostic hash.

### Proof of Concept

1. Observe alert `20230001` on mainnet — its raw bytes and two valid signatures are publicly known (they appear verbatim in `util/network-alert/src/tests/generate_alert_signature.rs` lines 73–75).
2. Construct the identical `packed::Alert` on testnet.
3. Call `send_alert` on any testnet node's RPC endpoint with this payload.
4. `Verifier::verify_signatures` computes `calc_alert_hash` over the same `RawAlert` bytes, recovers the same two Nervos Foundation public keys, meets the `signatures_threshold = 2` requirement, and returns `Ok(())`.
5. The alert is stored by the notifier and broadcast to all connected testnet peers — a mainnet alert has been successfully replayed on testnet with zero key access required. [8](#0-7) [9](#0-8)

### Citations

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

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

**File:** util/network-alert/src/verifier.rs (L56-64)
```rust
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

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
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
