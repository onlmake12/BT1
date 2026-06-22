### Title
Missing Chain-ID Domain Separator in `calc_alert_hash` Enables Cross-Network Alert Replay — (File: `util/gen-types/src/extension/calc_hash.rs`)

---

### Summary

`calc_alert_hash` computes the signing message for CKB network alerts as a plain `blake2b_256` of the serialized `RawAlert` bytes, with no chain ID or network identifier included. Because the same four alert public keys are compiled into every CKB binary by default, a valid alert signature produced for one CKB network (e.g., mainnet) is cryptographically indistinguishable from a valid alert for any other CKB network (e.g., testnet, staging, or a custom devnet). Any unprivileged RPC caller who has observed a legitimately signed alert on one network can replay it verbatim on another network via the `send_alert` RPC.

---

### Finding Description

`RawAlert` is defined in the Molecule schema as:

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

It contains no chain ID, no genesis hash, and no network name. The hash used as the signing message is computed as:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // = blake2b_256(self.as_slice())
}
``` [2](#0-1) 

The `Verifier` then uses this hash directly as the secp256k1 message:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [3](#0-2) 

The authorized public keys are compiled in from a single shared `alert_signature.toml` that is the same for all networks:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  ...
]
``` [4](#0-3) 

`NetworkAlertConfig::default()` embeds this file at compile time and is used for every network unless explicitly overridden:

```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
}
``` [5](#0-4) 

Because the signing message is `blake2b_256(raw_alert_bytes)` with no chain-specific domain separator, and the same keys are trusted on every network, a signature that is valid on mainnet is equally valid on testnet, staging, or any custom CKB network.

---

### Impact Explanation

An unprivileged attacker who observes any legitimately signed alert broadcast on one CKB network (alerts are relayed over the P2P layer and are publicly visible) can submit the identical `Alert` struct to any other CKB network via the `send_alert` RPC. The receiving node's `Verifier::verify_signatures` will accept it because the keys and the hash both match. The alert is then stored, propagated to all peers, and surfaced to node operators as a genuine critical warning. A real-world consequence is that mainnet node operators could receive a stale or context-incorrect alert (e.g., a version-range warning that does not apply to the mainnet release they are running), potentially triggering unnecessary emergency upgrades or operational disruption. Conversely, a testnet alert could be replayed on mainnet to create false urgency.

---

### Likelihood Explanation

All signed alerts are broadcast over the public P2P network and are also submittable via the `send_alert` RPC, which is open to any caller. No key material is required — only the ability to observe a past alert and call the RPC on a different network. The same default key set is compiled into every CKB binary, so no configuration knowledge is needed. The attack is fully passive until the replay step.

---

### Recommendation

Include a network-specific domain separator in the data that is hashed to produce the signing message. The natural choice is the genesis block hash, which is already available in `Consensus::identify_name()`:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])
}
``` [6](#0-5) 

The signing message should be computed as:

```
blake2b_256(genesis_hash || raw_alert_bytes)
```

This binds every alert signature to a specific chain instance, making cross-network replay cryptographically impossible. The `RawAlert` schema or the `calc_alert_hash` implementation should be updated accordingly, and the `Verifier` should receive the genesis hash at construction time.

---

### Proof of Concept

1. Run a CKB mainnet node and a CKB testnet node, both using the default compiled-in alert keys.
2. On mainnet, submit a valid alert (signed by 2-of-4 key holders) via `send_alert`. The alert is accepted and propagated.
3. Capture the raw `Alert` struct (available from any peer or from the mainnet node's RPC response).
4. Submit the identical `Alert` struct to the testnet node via `send_alert`.
5. The testnet `Verifier::verify_signatures` calls `alert.calc_alert_hash()` — which produces the same 32-byte hash because `RawAlert` contains no chain ID — and then calls `verify_m_of_n` against the same compiled-in public keys. Verification succeeds.
6. The testnet node stores the alert, relays it to all connected testnet peers, and surfaces it to testnet operators as a genuine critical warning, even though it was intended for mainnet. [7](#0-6) [8](#0-7)

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

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```
