### Title
Network Alert Signature Scheme Lacks Chain Domain Separation, Enabling Cross-Network Replay - (File: `util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system signs a `RawAlert` hash that contains no network or chain identifier. Because the same four Nervos Foundation signing keys are compiled into every CKB binary (mainnet, testnet, devnet), a valid alert captured from one network is cryptographically indistinguishable from a valid alert on any other network. Any unprivileged P2P peer can replay a legitimately signed testnet alert onto mainnet nodes, causing every receiving node to accept, store, and re-broadcast the replayed alert to all its peers.

---

### Finding Description

**Signed message contains no network domain separator.**

The `RawAlert` molecule schema is defined as:

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

There is no `network_id`, `chain_id`, or genesis-hash field. The alert hash is computed as a plain `blake2b_256` over the raw molecule bytes of this struct:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // = blake2b_256(self.as_slice())
}
``` [2](#0-1) 

The verifier signs and checks only this hash:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
// ...
verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [3](#0-2) 

**The same four public keys are compiled into every CKB binary.**

`NetworkAlertConfig::default()` loads from the embedded `alert_signature.toml`:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [4](#0-3) 

This default is used for all networks (mainnet Mirana, testnet Pudge, devnet) because it is a compile-time include:

```rust
let alert_config = include_bytes!("./alert_signature.toml");
toml::from_slice(&alert_config[..]).expect("alert system config")
``` [5](#0-4) 

**The P2P relay path accepts and re-broadcasts any alert that passes signature verification.**

`AlertRelayer::received()` processes every inbound P2P alert message. It verifies signatures and, on success, stores the alert and broadcasts it to all connected peers — with no check on which network the alert was originally intended for:

```rust
if let Err(err) = self.verifier.verify_signatures(&alert) {
    // ban peer
    return;
}
// mark sender as known
self.mark_as_known(peer_index, alert_id);
// broadcast message
nc.quick_filter_broadcast(TargetSession::Multi(...), data)
// add to received alerts
self.notifier.lock().add(&alert);
``` [6](#0-5) 

---

### Impact Explanation

An attacker who captures a legitimately signed testnet alert (all P2P traffic is observable) can submit it verbatim to any mainnet node via the P2P Alert protocol. Because the signature covers only the `RawAlert` content — which carries no network identifier — and the same four keys are trusted on every network, every mainnet node will:

1. Accept the alert as cryptographically valid.
2. Store it in its local notifier and display it to the node operator.
3. Re-broadcast it to all connected mainnet peers, causing network-wide propagation.

This undermines the integrity of the alert system as a trusted, network-scoped communication channel from the Nervos Foundation. Node operators on mainnet may act on stale, out-of-context, or misleading instructions (e.g., a testnet-specific "upgrade immediately" or "downgrade to version X" message), potentially destabilizing the mainnet node population.

---

### Likelihood Explanation

The preconditions are minimal:

- **No privileged access required.** The attacker only needs to observe P2P traffic on testnet (or any other CKB network) to capture a valid alert. Alerts are broadcast to all connected peers in plaintext molecule encoding.
- **No key material required.** The attacker does not forge signatures; they replay an existing, legitimately signed alert byte-for-byte.
- **No special network position required.** The attacker connects to any mainnet node as a normal P2P peer and sends the captured alert bytes. The `AlertRelayer::received()` handler processes it immediately.
- **Persistence.** Once accepted by one mainnet node, the alert propagates to the entire mainnet via the relay mechanism, requiring only a single successful injection.

The only limiting factor is that a valid alert must have been issued on another network at some point. Given that testnet is actively used for protocol testing and alerts have been issued historically, this condition is realistic.

---

### Recommendation

Include a network domain separator in the signed alert hash. The genesis block hash (already available in `ckb-chain-spec`) is the canonical per-network constant in CKB and is the appropriate binding value. The `RawAlert` schema should be extended with a `network_id` field (or the hash computation should prefix the genesis hash before hashing), so that:

```
alert_hash = blake2b_256(genesis_hash || raw_alert_bytes)
```

This ensures a signature produced for testnet is cryptographically invalid on mainnet, directly mirroring the recommendation in the reference report to bind signatures to the specific deployment instance.

---

### Proof of Concept

1. Run a CKB testnet node and connect to the testnet P2P network.
2. Observe an inbound `Alert` P2P message (molecule-encoded `packed::Alert` bytes). Any previously issued testnet alert suffices; historical alert `20230001` has known valid signatures.
3. Connect to a CKB mainnet node as a normal P2P peer (no authentication required).
4. Send the captured alert bytes verbatim over the Alert protocol channel.
5. The mainnet node's `AlertRelayer::received()` parses the message, calls `verifier.verify_signatures()`, recovers the two signing public keys from the ECDSA signatures, finds them in the compiled-in `pubkeys` set (identical on both networks), and passes the `verify_m_of_n` threshold check.
6. The alert is stored in the mainnet notifier and re-broadcast to all connected mainnet peers.
7. All reachable mainnet nodes display the testnet alert message to their operators.

The known testnet alert `20230001` with signatures `8dca2836...` and `4554b378...` (visible in `util/network-alert/src/tests/generate_alert_signature.rs`) can be used directly to demonstrate acceptance on any node running the default compiled-in key set. [7](#0-6)

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
