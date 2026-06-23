### Title
Network Alert Signature Replay Across CKB Forks Due to Missing Chain Domain Separator — (`util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system signs and verifies alert messages using only the hash of the `RawAlert` molecule struct. No chain identifier (genesis hash, network name, or chain spec hash) is included in the signed message. Because the same hardcoded public keys are used across all CKB networks (mainnet, testnet, devnet), a valid alert signature produced for one network is cryptographically valid on every other CKB network. An unprivileged P2P peer can replay a mainnet alert on testnet nodes (or vice versa) and it will pass signature verification and be broadcast network-wide.

---

### Finding Description

The `verify_signatures` function in `util/network-alert/src/verifier.rs` constructs the message to verify as:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [1](#0-0) 

`calc_alert_hash` delegates to `calc_hash`, which is simply `blake2b_256(self.as_slice())` — a plain hash of the raw molecule bytes of `RawAlert`: [2](#0-1) 

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
``` [3](#0-2) 

There is no `chain_id`, genesis hash, network name, or any other network-binding field in the signed payload. The `NetworkAlertConfig::default()` loads the same hardcoded public keys for all networks from the bundled `alert_signature.toml`: [4](#0-3) [5](#0-4) 

Because the signed hash is purely a function of the alert content (not the network), and the same keys are trusted on all networks, any alert signed for one CKB network is a valid alert on every other CKB network.

The `alert_relayer.rs` receives alert messages from any P2P peer, calls `verifier.verify_signatures`, and if it passes, broadcasts the alert to all connected peers: [6](#0-5) 

The `send_alert` RPC also accepts alerts from any local RPC caller and broadcasts them after signature verification: [7](#0-6) 

---

### Impact Explanation

An attacker who obtains a legitimately signed mainnet alert (alerts are broadcast publicly across the entire P2P network) can relay it verbatim to testnet nodes. The testnet nodes will:
1. Verify the signature — it passes, because the same keys are trusted and the hash is network-agnostic.
2. Store the alert in the local notifier.
3. Re-broadcast it to all connected testnet peers.

The result is that a mainnet alert (e.g., "CKB v0.105.* have bugs, upgrade immediately") propagates across the testnet network and is displayed to all testnet node operators. The reverse is also true: a test alert or a lower-priority alert signed for testnet can be replayed on mainnet, potentially causing mainnet operators to take incorrect operational decisions (unnecessary upgrades, ignoring a real alert, or panic). The `identify_name` function does bind the P2P network name to the genesis hash for peer connection filtering, but the Alert protocol message itself carries no such binding in its signed payload. [8](#0-7) 

---

### Likelihood Explanation

All signed mainnet alerts are publicly observable on the P2P network. Any node connected to mainnet receives them. An attacker simply needs to be connected to both mainnet and testnet (or any two CKB networks sharing the default public keys) and forward the raw alert bytes. No key material, no privileged access, and no special tooling is required — the attacker only needs to relay a received `packed::Alert` message to a peer on a different network via the `/ckb/alt` protocol.

---

### Recommendation

Include a network-binding domain separator in the signed alert message. The natural candidate is the genesis block hash, which is already computed and available via `consensus.genesis_hash()` and used in `identify_name`. The `RawAlert` molecule schema should be extended with a `chain_id` or `genesis_hash` field, or the `calc_alert_hash` implementation should prefix the hash input with the genesis hash before signing and verifying. Alternatively, the verifier can be initialized with the local genesis hash and incorporate it into the message digest:

```rust
// Example fix in verifier.rs
let mut hasher = new_blake2b();
hasher.update(self.genesis_hash.as_slice()); // domain separator
hasher.update(alert.calc_alert_hash().as_slice());
let mut message_bytes = [0u8; 32];
hasher.finalize(&mut message_bytes);
let message = Message::from_slice(&message_bytes)?;
```

---

### Proof of Concept

1. Run a CKB mainnet node and a CKB testnet node.
2. On the mainnet node, observe a broadcast alert via the `/ckb/alt` P2P protocol (or via `get_blockchain_info` RPC which returns active alerts). Capture the raw `packed::Alert` bytes.
3. Connect a custom P2P client to the testnet node and send the captured `packed::Alert` bytes over the `/ckb/alt` protocol.
4. The testnet node's `alert_relayer.rs` calls `verifier.verify_signatures` — it passes because `calc_alert_hash` produces the same 32-byte digest on both networks and the same hardcoded public keys are trusted.
5. The testnet node stores the alert and re-broadcasts it to all its testnet peers.
6. All testnet nodes display the mainnet alert message to their operators.

The root cause is confirmed at:
- `util/network-alert/src/verifier.rs:35` — message is `blake2b_256(raw_alert_bytes)` with no network binding.
- `util/gen-types/src/extension/calc_hash.rs:296-298` — `calc_alert_hash` = `calc_hash` = `blake2b_256(self.as_slice())`.
- `util/gen-types/schemas/extensions.mol:445-453` — `RawAlert` schema has no chain identifier field.
- `util/app-config/src/configs/alert_signature.toml:1-8` — same public keys used across all networks.

### Citations

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
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

**File:** util/network-alert/src/alert_relayer.rs (L150-178)
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
