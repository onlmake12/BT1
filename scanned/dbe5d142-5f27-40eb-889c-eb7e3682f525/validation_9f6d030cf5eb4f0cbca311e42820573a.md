### Title
Cross-Network Replay of Signed Network Alerts Due to Missing Chain Domain Separator — (File: `util/network-alert/src/verifier.rs`)

### Summary

The `RawAlert` signed message in CKB's network alert system contains no chain/network identifier. The same four hardcoded Nervos Foundation signing keys are used across all CKB networks (mainnet, testnet, devnet). An unprivileged attacker connected to both mainnet and testnet can capture a valid alert broadcast on testnet and replay it verbatim on mainnet — where it will pass signature verification and be accepted and propagated to all mainnet nodes.

### Finding Description

**Root cause — no domain separator in the signed payload:**

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
``` [1](#0-0) 

There is no `chain_id`, `genesis_hash`, or any network-scoping field. The alert hash is computed directly from these bytes:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()
}
``` [2](#0-1) 

The verifier signs and verifies against this hash with no network context:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
// ...
verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [3](#0-2) 

**Same keys used on all networks:**

The four signing public keys are hardcoded in a single config file loaded as the `Default` for `NetworkAlertConfig`, meaning every CKB network (mainnet, testnet, devnet) uses the identical key set:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
``` [4](#0-3) 

The `Default` impl embeds this file at compile time:

```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
}
``` [5](#0-4) 

**Exploit flow:**

1. Attacker connects to both CKB mainnet and testnet P2P networks (no privilege required — both are public).
2. The Nervos Foundation signs and broadcasts a testnet-specific alert (e.g., `"CKB v0.X.* have a critical bug on testnet. Please upgrade."`).
3. Attacker captures the raw `packed::Alert` bytes from the testnet P2P stream.
4. Attacker submits the identical alert to a mainnet node via the public `send_alert` RPC endpoint, or relays it directly over the mainnet P2P protocol.
5. The mainnet `Verifier::verify_signatures` computes `calc_alert_hash()` over the same `RawAlert` bytes, recovers the same two-of-four Foundation keys, and accepts the alert.
6. The `AlertRelayer` propagates the accepted alert to all connected mainnet peers. [6](#0-5) 

### Impact Explanation

Every mainnet full node displays the replayed alert message to operators and users. The alert system is the designated mechanism for communicating critical protocol bugs. A replayed testnet alert on mainnet can:

- Instruct mainnet users to upgrade to a version that is unnecessary or even harmful on mainnet.
- Instruct mainnet users to stop using a version range that is perfectly safe on mainnet.
- Suppress a real mainnet alert: if the replayed alert carries the same `id` as a pending genuine mainnet alert, the `Notifier` deduplicates by `id` and the real alert is silently dropped. [7](#0-6) 

The third sub-impact (id collision suppression) is the most severe: an attacker who observes a testnet alert with `id = N` can pre-empt a future mainnet alert with the same `id`, preventing the Foundation's genuine warning from reaching mainnet users.

### Likelihood Explanation

- **No privilege required.** The attacker only needs a standard CKB node connected to both mainnet and testnet, both of which are public.
- **Trigger condition is realistic.** The Foundation has historically signed testnet alerts (e.g., alert `20230001` visible in the test fixtures). Any such alert is immediately replayable on mainnet.
- **No cryptographic break required.** The attacker reuses an already-valid signature; they do not need to forge one.
- **`send_alert` RPC is publicly documented and enabled** on nodes that include the `Alert` RPC module. [8](#0-7) 

### Recommendation

Add a network domain separator to the signed payload. The simplest approach is to include the genesis block hash (already available as `Consensus::genesis_hash`) in the `RawAlert` structure or as a prefix to the data hashed by `calc_alert_hash`. This makes a testnet-signed alert cryptographically invalid on mainnet because the genesis hashes differ.

Alternatively, include the chain spec name (`"ckb"` / `"ckb_testnet"`) as a fixed prefix before hashing, mirroring the pattern already used in `Consensus::identify_name`:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])
}
``` [9](#0-8) 

### Proof of Concept

```
# Step 1 – observe a valid testnet alert (e.g., from P2P or RPC get_blockchain_info)
# Suppose the Foundation broadcasts on testnet:
#   id=42, message="Upgrade immediately", signatures=[sig_A, sig_B]

# Step 2 – submit the identical alert to a mainnet node's RPC
curl -X POST http://<mainnet-node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc":"2.0","method":"send_alert","id":1,
    "params":[{
      "id":"0x2a",
      "cancel":"0x0",
      "priority":"0x1",
      "message":"Upgrade immediately",
      "notice_until":"0x<future_ts>",
      "signatures":["0x<sig_A>","0x<sig_B>"]
    }]
  }'

# Expected result: {"jsonrpc":"2.0","result":null,"id":1}
# The alert is accepted, stored, and relayed to all mainnet peers.
# get_blockchain_info on any mainnet node now shows the replayed testnet alert.
```

The acceptance is guaranteed because `calc_alert_hash` is a pure function of the `RawAlert` fields, which are identical on both networks, and the verifier key set is identical on both networks. [7](#0-6) [10](#0-9)

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

**File:** rpc/src/module/alert.rs (L14-19)
```rust
/// RPC Module Alert for network alerts.
///
/// An alert is a message about critical problems to be broadcast to all nodes via the p2p network.
///
/// The alerts must be signed by 2-of-4 signatures, where the public keys are hard-coded in the source code
/// and belong to early CKB developers.
```

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```
