### Title
Missing Chain/Network Domain Separator in `RawAlert` Hash Enables Cross-Network Alert Replay - (File: `util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system signs and verifies alerts using a hash of `RawAlert` bytes. The `RawAlert` structure contains no chain or network identifier, so a valid alert signature produced for one CKB network (e.g., mainnet) is cryptographically identical and fully accepted on any other CKB network (e.g., testnet, devnet) that uses the same hardcoded signing keys. Any unprivileged P2P peer who observes a valid alert on one network can relay it verbatim to nodes on a different network, where it will pass signature verification and be broadcast to all connected peers.

---

### Finding Description

**Root cause — no domain separator in the signed hash:**

`calc_alert_hash()` is defined as a plain `blake2b_256` over the raw molecule-serialized bytes of `RawAlert`:

```rust
// util/gen-types/src/extension/calc_hash.rs
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
``` [1](#0-0) 

The `RawAlert` molecule schema contains only application-level fields:

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
``` [2](#0-1) 

There is no chain ID, genesis block hash, or network name anywhere in the signed payload. The verifier hashes exactly this structure and checks it against the configured public keys:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    // ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [3](#0-2) 

**Same keys used across all networks by default:**

`NetworkAlertConfig::default()` embeds the same four Nervos Foundation public keys for every CKB node regardless of which network it runs on:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [4](#0-3) 

```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
}
``` [5](#0-4) 

**Deduplication does not prevent cross-network replay:**

The only replay guard is an in-memory `has_received(alert_id)` check per node:

```rust
if self.notifier.lock().has_received(alert_id) {
    return;
}
``` [6](#0-5) 

This check is per-node and per-network. A testnet node that has never seen mainnet alert ID `20230001` will not deduplicate it; it will pass signature verification (same keys, same hash) and be accepted and re-broadcast to all connected testnet peers.

---

### Impact Explanation

An attacker who captures any valid, in-flight alert from CKB mainnet (observable via P2P or the `get_blockchain_info` RPC) can relay it byte-for-byte to CKB testnet (or any other CKB network using the default keys). The receiving nodes will:

1. Parse the alert successfully.
2. Pass `verify_signatures` — the hash is identical and the keys are the same.
3. Accept the alert into their notifier and re-broadcast it to all connected peers.
4. Display the alert message to node operators.

The concrete impact is that node operators on one network receive alerts that were intended for a different network. A mainnet "critical bug — upgrade immediately" alert replayed on testnet causes unnecessary panic and disruption among testnet operators. Conversely, a low-urgency testnet alert replayed on mainnet could be used to desensitize mainnet operators to alert messages, undermining the alert system's purpose as a last-resort emergency communication channel. The alert system is explicitly described as a mechanism to "reach consensus offline under critical bugs." [7](#0-6) 

---

### Likelihood Explanation

The attack requires no privileged access. Any node that participates in both the mainnet and testnet P2P networks (or any two CKB networks sharing the default keys) can perform the replay. Valid signed alerts are publicly observable on the P2P network and via the `send_alert` / `get_blockchain_info` RPC. The real alert `20230001` with its two valid signatures is even embedded in the test suite, demonstrating that valid signed alerts are not secret. [8](#0-7) 

---

### Recommendation

Include a network-identifying domain separator in the data that is hashed and signed. The natural choice is the genesis block hash (already available in `ckb-shared`), which is unique per network. The `calc_alert_hash` implementation should prefix the `RawAlert` bytes with the genesis hash before hashing:

```rust
pub fn calc_alert_hash_with_genesis(raw: &packed::RawAlert, genesis_hash: &packed::Byte32) -> packed::Byte32 {
    let mut hasher = new_blake2b();
    hasher.update(genesis_hash.as_slice());
    hasher.update(raw.as_slice());
    let mut result = [0u8; 32];
    hasher.finalize(&mut result);
    result.into()
}
```

The `Verifier` would need to receive the genesis hash at construction time and use it in `verify_signatures`. The `AlertRelayer` and the `send_alert` RPC handler would pass the genesis hash from the shared state.

---

### Proof of Concept

1. Run a CKB mainnet node and a CKB testnet node, both using `NetworkAlertConfig::default()`.
2. On mainnet, observe a valid alert (e.g., alert `20230001` with its two known signatures from the test suite) via the P2P protocol or RPC.
3. Connect to a testnet node as a P2P peer using the Alert protocol (`SupportProtocols::Alert`).
4. Send the raw alert bytes (identical to the mainnet alert) to the testnet node.
5. The testnet node calls `verify_signatures`, computes `blake2b_256(raw_alert_bytes)` — identical to the mainnet hash — and verifies against the same four hardcoded public keys. Verification succeeds.
6. The testnet node adds the alert to its notifier and broadcasts it to all connected testnet peers.
7. All testnet node operators see the mainnet alert message displayed on their testnet nodes. [3](#0-2) [1](#0-0) [5](#0-4)

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

**File:** util/network-alert/src/alert_relayer.rs (L1-8)
```rust
//! AlertRelayer
//! We implement a Bitcoin like alert system, n of m alert key holders can decide to send alert
//messages to all client
//! to leave a space to reach consensus offline under critical bugs
//!
//! A cli to generate alert message,
//! A config option to set alert messages to broadcast.
//
```

**File:** util/network-alert/src/alert_relayer.rs (L147-149)
```rust
        if self.notifier.lock().has_received(alert_id) {
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
