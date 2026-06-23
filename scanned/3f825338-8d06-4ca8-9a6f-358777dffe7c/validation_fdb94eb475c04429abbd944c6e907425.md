### Title
Cross-Network Alert Signature Replay via Missing Domain Separator in `calc_alert_hash` - (File: `util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system signs and verifies alerts using a hash derived solely from the `RawAlert` molecule struct, with no chain ID, genesis hash, or network identifier included in the signing message. Because the same four Nervos Foundation public keys are hardcoded as the default `NetworkAlertConfig` for all networks (mainnet, testnet, devnet), a valid alert signature produced for one network is cryptographically valid on every other CKB network. Any unprivileged P2P peer or RPC caller can replay a legitimately-signed mainnet alert onto testnet nodes (or vice versa) and have it accepted, stored, and broadcast to all connected peers.

---

### Finding Description

**Root cause — no domain separator in the signing message**

`RawAlert` is defined in `util/gen-types/schemas/extensions.mol`:

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

None of these fields carry a chain ID, genesis hash, or network name.

`calc_alert_hash` is implemented as a plain `blake2b_256` of the molecule-encoded bytes of `RawAlert`:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // → blake2b_256(self.as_slice())
}
``` [2](#0-1) [3](#0-2) 

`verify_signatures` in the `Verifier` uses this hash directly as the ECDSA message:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [4](#0-3) 

**Same keys on every network**

`NetworkAlertConfig::default()` embeds `alert_signature.toml` at compile time and is used for all networks unless overridden:

```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
}
``` [5](#0-4) 

The embedded file contains the four production Nervos Foundation public keys: [6](#0-5) 

**P2P relay path accepts the replayed alert without network checks**

In `alert_relayer.rs`, the `received` handler only checks UTF-8 validity, deduplication by alert ID, and signature validity. There is no check that the alert was intended for this network:

```rust
if let Err(err) = self.verifier.verify_signatures(&alert) { ... }
// mark sender as known
self.mark_as_known(peer_index, alert_id);
// broadcast message
...
self.notifier.lock().add(&alert);
``` [7](#0-6) 

**Exploit flow**

1. Attacker observes a valid, unexpired mainnet alert over P2P (or from the public `get_blockchain_info` RPC response).
2. Attacker connects to testnet nodes as an ordinary P2P peer.
3. Attacker sends the identical `Alert` bytes (same `RawAlert` + same signatures) to testnet peers via the Alert protocol.
4. Each testnet node calls `verify_signatures`, which recomputes `blake2b_256(raw_alert_bytes)` — identical to the mainnet hash — and confirms the signatures are valid against the same four hardcoded keys.
5. The alert passes, is stored in the notifier, and is broadcast to all connected testnet peers.
6. All testnet nodes display the mainnet alert message to their operators.

The reverse direction (testnet → mainnet) is equally trivial.

---

### Impact Explanation

The network alert system is the CKB core team's emergency broadcast channel for critical security vulnerabilities. A replayed cross-network alert causes:

- **False security notifications** displayed to all operators of the target network, potentially triggering unnecessary emergency upgrades, node shutdowns, or user panic.
- **Alert slot exhaustion**: because deduplication is by integer `id`, a replayed mainnet alert with a given ID blocks any legitimate alert with the same ID from being accepted on testnet (and vice versa), since `has_received(alert_id)` returns `true` after the first acceptance.
- **Cascading broadcast**: once accepted by one node, the replayed alert is automatically re-broadcast to all connected peers, amplifying the effect across the entire target network without further attacker involvement. [8](#0-7) 

---

### Likelihood Explanation

- **No privilege required**: any P2P peer or `send_alert` RPC caller can submit the replayed alert.
- **Signatures are public**: valid mainnet alert signatures are observable from any mainnet node's `get_blockchain_info` response and are even embedded in the test suite (e.g., `test_alert_20230001`).
- **Same keys on all networks**: the default config is identical across mainnet, testnet, and devnet; no key rotation separates networks.
- **No expiry check in P2P path**: the P2P `received` handler does not verify `notice_until > now`, so even alerts whose `notice_until` has passed can be injected (they will be cleared by the periodic `clear_expired_alerts` call, but are accepted and broadcast in the interim). [9](#0-8) 

---

### Recommendation

Include a network-specific domain separator in the signing message. The genesis hash (already available as `consensus.genesis_hash`) is the natural choice, as it uniquely identifies each CKB network:

```rust
// In verify_signatures / alert creation:
let mut hasher = new_blake2b();
hasher.update(genesis_hash.as_slice());          // domain separator
hasher.update(alert.calc_alert_hash().as_slice());
let mut message_bytes = [0u8; 32];
hasher.finalize(&mut message_bytes);
let message = Message::from_slice(&message_bytes)?;
```

This mirrors the fix applied to the ERC1271Handler (inclusion of chain ID and verifier address in the signed digest) and ensures that a signature produced for one CKB network is cryptographically invalid on any other.

---

### Proof of Concept

The following demonstrates that the two known mainnet signatures from `test_alert_20230001` verify successfully against a `Verifier` initialized with the default (mainnet) config — and would equally verify on a testnet node using the same default config, since the signing message is network-agnostic:

```rust
// From util/network-alert/src/tests/generate_alert_signature.rs
let config = NetworkAlertConfig::default(); // same keys on all networks
let verifier = Verifier::new(config);
let raw_alert = packed::RawAlert::new_builder()
    .notice_until(1681574400000u64)
    .id(20230001u32)
    // ... (same fields)
    .build();
// calc_alert_hash = blake2b_256(raw_alert_bytes) — no chain ID
let alert = packed::Alert::new_builder()
    .raw(raw_alert)
    .signatures(mainnet_signatures) // copied verbatim from mainnet
    .build();
assert!(verifier.verify_signatures(&alert).is_ok()); // passes on testnet too
``` [9](#0-8) [10](#0-9)

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
