### Title
Network Alert Signature Replay Across CKB Networks — (`util/network-alert/src/verifier.rs`)

---

### Summary

The `RawAlert` signing message produced by `calc_alert_hash()` contains no chain ID or network identifier. Because the same four Nervos Foundation public keys are hard-coded as the default alert signers for all CKB networks, a cryptographically valid alert signed for testnet is also valid on mainnet. Any unprivileged RPC caller who observes a live testnet alert can replay it verbatim against a mainnet node's `send_alert` endpoint, causing the false alert to propagate across the entire mainnet P2P network.

---

### Finding Description

The `RawAlert` Molecule schema defines the signed payload:

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

`calc_alert_hash()` hashes only the raw bytes of this struct:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()
}
``` [2](#0-1) 

`Verifier::verify_signatures` uses this hash directly as the signing message with no domain separation:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
// ...
verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [3](#0-2) 

The default signing keys are identical across all CKB networks — they are embedded at compile time from a single config file:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [4](#0-3) 

`NetworkAlertConfig::default()` loads this file at compile time, so every CKB node (mainnet, testnet, devnet) trusts the same four keys unless the operator explicitly overrides the config. [5](#0-4) 

The `send_alert` RPC endpoint accepts an alert from any caller, verifies the signatures, and immediately broadcasts the alert to all connected peers:

```rust
fn send_alert(&self, alert: Alert) -> Result<()> {
    // ...
    let result = self.verifier.verify_signatures(&alert);
    match result {
        Ok(()) => {
            self.notifier.lock().add(&alert);
            self.network_controller.broadcast_with_handle(
                SupportProtocols::Alert.protocol_id(),
                alert.as_bytes(),
                &self.handle,
            );
            Ok(())
        }
        // ...
    }
}
``` [6](#0-5) 

No caller authentication beyond the alert signatures themselves is required.

---

### Impact Explanation

An attacker who captures a legitimately signed alert from testnet (e.g., a test alert the Foundation signed to exercise the alert pipeline, or a real testnet advisory) can submit it unchanged to any mainnet node's `send_alert` RPC. The signature verification passes because the hash covers only the alert content — not the network — and the same keys are trusted on mainnet. The alert is then relayed peer-to-peer across the entire mainnet. Users receive a false emergency message (e.g., "upgrade immediately", "critical bug in version X.Y") that was never intended for mainnet, potentially triggering mass node downgrades, user panic, or loss of confidence in the network.

---

### Likelihood Explanation

The preconditions are low-effort:

1. The Nervos Foundation routinely signs alerts on testnet to test the alert pipeline (as evidenced by the test infrastructure in the codebase).
2. Testnet alerts are observable by any network participant — they propagate over the public P2P network.
3. The `send_alert` RPC requires no authentication beyond the alert signatures, which the attacker already possesses verbatim.
4. The same four default public keys are compiled into every CKB release for all networks.

The only barrier is that the attacker must wait for a legitimate testnet alert to be signed. Given that the Foundation has signed at least one real alert (`id: 20230001`) and uses the same keys everywhere, this is a realistic scenario.

---

### Recommendation

**Short term:** Include a network domain separator in the signed message. The genesis block hash is the canonical per-network identifier in CKB and is already available at alert-signing time. Compute the alert hash as:

```
alert_hash = blake2b(genesis_hash || raw_alert_bytes)
```

This ensures a signature produced for testnet (genesis hash A) is cryptographically invalid on mainnet (genesis hash B).

**Long term:** Treat the alert signing key set as network-scoped. Publish separate key sets per network in the default config, and include the network genesis hash in the signed domain so that even if key sets overlap, signatures cannot cross networks.

---

### Proof of Concept

1. Run a CKB testnet node and observe a valid alert propagating over the P2P network. Capture the full `Alert` molecule struct (raw bytes + two valid signatures).

2. Submit the captured alert unchanged to a mainnet node with the Alert RPC module enabled:

```bash
curl -X POST http://mainnet-node:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "send_alert",
    "id": 1,
    "params": [{
      "id": "<testnet_alert_id>",
      "cancel": "0x0",
      "priority": "0x14",
      "message": "<testnet alert message>",
      "notice_until": "<future_timestamp>",
      "signatures": [
        "<testnet_sig_1>",
        "<testnet_sig_2>"
      ]
    }]
  }'
```

3. The mainnet node calls `Verifier::verify_signatures`, which computes `calc_alert_hash()` over the `RawAlert` bytes — identical on both networks since the content is the same — and recovers the same two Foundation public keys. The 2-of-4 threshold is met.

4. The alert is accepted, stored in the notifier, and broadcast to all mainnet peers via `SupportProtocols::Alert`. The false alert propagates network-wide.

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

**File:** util/gen-types/src/extension/calc_hash.rs (L296-298)
```rust
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
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
