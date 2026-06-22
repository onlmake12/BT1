### Title
Network Alert Signature Replay Across Chains Due to Missing Chain-Specific Domain Separation — (`util/network-alert/src/verifier.rs`, `util/gen-types/src/extension/calc_hash.rs`)

---

### Summary

The CKB network alert system signs and verifies alert messages using a hash computed solely from the `RawAlert` struct contents. The `RawAlert` schema contains no chain identifier (no genesis hash, no network name, no chain ID). As a result, a valid alert signature produced for one CKB network (e.g., mainnet) is cryptographically valid on any other CKB network that uses the same alert public keys — including a hard-forked chain that starts with the same default configuration.

---

### Finding Description

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

No chain identifier, genesis hash, or network name is included. [1](#0-0) 

`calc_alert_hash()` is implemented as a plain `blake2b_256` over `self.as_slice()` — the raw bytes of `RawAlert` with no domain separation:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
``` [2](#0-1) 

`verify_signatures()` in the verifier uses this hash directly as the signed message:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [3](#0-2) 

The verifier holds a static set of public keys loaded from the bundled `alert_signature.toml` at startup: [4](#0-3) 

Because the signed message is purely a function of the alert content and contains no chain-specific context, any alert that passes verification on one network will pass verification on any other network sharing the same alert public keys.

---

### Impact Explanation

**Cross-network alert replay**: An attacker who observes a valid alert on CKB mainnet (captured from the P2P network or from the `get_blockchain_info` RPC response which returns `alerts`) can submit the identical alert bytes to a testnet, devnet, or hard-forked CKB node. The receiving node's `verify_signatures()` will accept it, add it to its notifier, and re-broadcast it across that network. [5](#0-4) 

**Cancel-alert weaponization**: The `cancel` field in `RawAlert` causes the notifier to suppress a previously received alert by ID. An attacker can replay a cancel-alert from one network to silence a legitimate active alert on another network, preventing nodes from displaying a real security warning. [6](#0-5) 

**Hard fork scenario**: If CKB undergoes a hard fork, the forked chain initially ships with the same bundled `alert_signature.toml` and the same alert public keys. Every alert ever signed for the original chain is immediately replayable on the fork, and vice versa, for the entire lifetime of the fork unless operators manually reconfigure the keys.

---

### Likelihood Explanation

The attacker requires no privileged access. Any P2P peer connected to a CKB node receives broadcast alerts. Alternatively, the `get_blockchain_info` RPC (unauthenticated by default) returns all active alerts. The attacker only needs to:

1. Collect a valid alert from one network (zero-effort, passive observation).
2. Submit it to a node on the target network via the `send_alert` RPC or by relaying it as a P2P message.

The `send_alert` RPC is the direct injection point: [7](#0-6) 

The P2P injection point is `AlertRelayer::received()`, which accepts alert messages from any connected peer: [8](#0-7) 

---

### Recommendation

Include a chain-specific domain separator in the signed message. The natural candidate in CKB is the genesis block hash, which is already used for network identification (`identify_name()`): [9](#0-8) 

Modify `calc_alert_hash()` to incorporate the genesis hash:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self, genesis_hash: &packed::Byte32) -> packed::Byte32 {
        let mut blake2b = new_blake2b();
        blake2b.update(genesis_hash.as_slice());
        blake2b.update(self.as_slice());
        let mut ret = [0u8; 32];
        blake2b.finalize(&mut ret);
        ret.into()
    }
}
```

The genesis hash must be threaded through `Verifier::verify_signatures()` and `AlertRelayer::received()`. This ensures that an alert signed for mainnet produces a different hash on testnet or any fork, making cross-network replay cryptographically impossible.

---

### Proof of Concept

1. On CKB mainnet, observe an active alert via:
   ```
   curl -X POST http://mainnet-node:8114 -d '{"jsonrpc":"2.0","method":"get_blockchain_info","params":[],"id":1}'
   ```
   Extract the `signatures` and all `RawAlert` fields from the response.

2. Reconstruct the identical `packed::Alert` bytes (same `id`, `cancel`, `priority`, `notice_until`, `message`, `min_version`, `max_version`, `signatures`).

3. Submit to a testnet node (or any CKB fork node with the same alert keys):
   ```
   curl -X POST http://testnet-node:8114 -d '{"jsonrpc":"2.0","method":"send_alert","params":[<replayed_alert>],"id":1}'
   ```

4. The testnet node calls `verify_signatures()`, computes `blake2b_256(raw_alert.as_slice())` — identical to the mainnet hash — and accepts the alert. It then broadcasts it to all connected peers on the testnet.

The `notice_until` timestamp check in `send_alert` only rejects already-expired alerts; any alert still within its validity window passes this gate: [10](#0-9)

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

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L98-178)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let alert: packed::Alert = match packed::AlertReader::from_slice(&data) {
            Ok(alert) => {
                if alert.raw().message().is_utf8()
                    && alert
                        .raw()
                        .min_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                    && alert
                        .raw()
                        .max_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                {
                    alert.to_entity()
                } else {
                    info!(
                        "A malformed message fromP peer {} : not utf-8 string",
                        peer_index
                    );
                    nc.ban_peer(
                        peer_index,
                        BAD_MESSAGE_BAN_TIME,
                        String::from("send us a malformed message: not utf-8 string"),
                    );
                    return;
                }
            }
            Err(err) => {
                info!("A malformed message from peer {}: {:?}", peer_index, err);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
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
```

**File:** util/network-alert/src/notifier.rs (L93-104)
```rust
    pub fn add(&mut self, alert: &Alert) {
        let alert_id = alert.raw().id().into();
        let alert_cancel = alert.raw().cancel().into();
        if self.has_received(alert_id) {
            return;
        }
        // checkout cancel_id
        if alert_cancel > 0 {
            self.cancel(alert_cancel);
        }
        // add to received alerts
        self.received_alerts.insert(alert_id, alert.clone());
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
