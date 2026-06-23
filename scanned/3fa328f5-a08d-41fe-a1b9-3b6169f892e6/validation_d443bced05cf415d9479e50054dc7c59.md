### Title
Missing Chain Identifier in Alert Hash Enables Cross-Chain Signature Replay - (File: `util/network-alert/src/verifier.rs`)

### Summary

The `RawAlert` message hash used for multi-signature verification in CKB's network alert system does not include any chain-specific domain separator (genesis hash, chain ID, or network identifier). A valid alert signed by Nervos Foundation key-holders for one CKB chain (e.g., testnet) can be replayed verbatim on any other CKB chain (e.g., mainnet) by an unprivileged attacker who simply observes the P2P network or queries `get_blockchain_info`. This is the direct analog of the reported missing-nonce/missing-chainid signature replay vulnerability.

---

### Finding Description

**Root cause — `calc_alert_hash` hashes only `RawAlert` content, no chain context:**

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

`calc_alert_hash()` is implemented as a straight hash of the raw serialized bytes of `RawAlert` with no additional domain data:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()
}
``` [2](#0-1) 

**Verification path — `Verifier::verify_signatures` uses this hash directly:**

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
        .map_err(|err| err.kind())?;
    Ok(())
}
``` [3](#0-2) 

No genesis hash, chain ID, or network name is mixed into `message` before verification. The `RawAlert` fields (`id`, `message`, `notice_until`, etc.) are identical in structure across all CKB chains.

**Attacker-controlled entry path — `send_alert` RPC and P2P `AlertRelayer`:**

The `send_alert` RPC accepts any `Alert` from any caller, verifies signatures, and broadcasts it to all peers:

```rust
fn send_alert(&self, alert: Alert) -> Result<()> {
    ...
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
        ...
    }
}
``` [4](#0-3) 

The P2P `AlertRelayer` also accepts and re-broadcasts alerts received from any peer after signature verification, with no chain context check: [5](#0-4) 

**Exploit flow:**

1. Nervos Foundation signs and broadcasts alert `{id: 42, message: "Upgrade now", notice_until: T}` on testnet. The signed bytes are publicly visible on the P2P network or via `get_blockchain_info`.
2. An unprivileged attacker copies the exact `Alert` struct (raw bytes + signatures) and submits it to a mainnet node via `send_alert` RPC, or injects it directly into the mainnet P2P Alert protocol.
3. `Verifier::verify_signatures` computes `blake2b(RawAlert_bytes)` — identical on both chains since `RawAlert` contains no chain-specific field — and the Foundation signatures verify successfully.
4. The mainnet `Notifier` stores and propagates the alert to all connected mainnet peers. All mainnet nodes display the replayed alert.

The `Notifier` deduplicates only by `alert_id` per node:

```rust
if self.has_received(alert_id) {
    return;
}
``` [6](#0-5) 

This means any alert ID not previously seen on the target chain is accepted without question.

---

### Impact Explanation

An unprivileged attacker can inject a legitimately-signed alert from one CKB chain onto any other CKB chain. Concretely:

- A testnet alert ("CKB v0.X has a critical bug, upgrade immediately") replayed on mainnet causes all mainnet nodes to display the message and trigger any configured `network_alert_notify_script`, potentially causing mass unnecessary upgrades, service disruptions, or user panic.
- A pre-fork alert replayed post-fork on the minority chain can suppress or confuse legitimate alerts on that chain.
- The `notify_controller.notify_network_alert` call executes an operator-configured shell script with the alert message as argument, amplifying the impact. [7](#0-6) 

**Impact: Medium** — No direct fund theft, but integrity of the network-wide emergency communication channel is broken; can cause widespread incorrect operator/user behavior.

---

### Likelihood Explanation

**Likelihood: Medium**

- Requires a valid alert to exist on any CKB chain (testnet alerts are issued periodically; historical alerts are hardcoded in tests).
- Alert bytes are publicly broadcast over the P2P network and queryable via `get_blockchain_info` — zero privilege required to obtain them.
- Submission via `send_alert` RPC requires only local RPC access (default: localhost), which is the standard operator interface.
- The attack is fully deterministic and requires no brute force or cryptographic break.

---

### Recommendation

Include a chain-specific domain separator in the signed message hash. The natural choice is the genesis block hash, already available as `consensus.genesis_hash` and used in the network identify name:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])
}
``` [8](#0-7) 

Concretely, extend `RawAlert` with a `chain_id` / `genesis_hash` field, or prepend the genesis hash to the data hashed by `calc_alert_hash()`, so that a signature produced for one chain is cryptographically invalid on any other chain.

---

### Proof of Concept

```
# Step 1: Obtain a valid alert from testnet (publicly visible)
curl -X POST http://testnet-node:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_blockchain_info","params":[],"id":1}'
# Extract alerts[0] from response

# Step 2: Replay the exact same alert on mainnet
curl -X POST http://mainnet-node:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "send_alert",
    "params": [{
      "id": "<testnet_alert_id>",
      "cancel": "0x0",
      "priority": "0x1",
      "message": "<testnet_alert_message>",
      "notice_until": "<testnet_notice_until>",
      "signatures": ["<sig1_from_testnet>", "<sig2_from_testnet>"]
    }],
    "id": 1
  }'
# Result: mainnet node accepts, stores, and broadcasts the replayed alert
# verify_signatures passes because calc_alert_hash() produces the same hash on both chains
```

The `Verifier::verify_signatures` call at `util/network-alert/src/verifier.rs:35` will succeed because `calc_alert_hash()` at `util/gen-types/src/extension/calc_hash.rs:296` hashes only the chain-agnostic `RawAlert` bytes, and the Foundation signatures are valid for that hash on any chain. [3](#0-2) [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L98-179)
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
    }
```

**File:** util/network-alert/src/notifier.rs (L93-98)
```rust
    pub fn add(&mut self, alert: &Alert) {
        let alert_id = alert.raw().id().into();
        let alert_cancel = alert.raw().cancel().into();
        if self.has_received(alert_id) {
            return;
        }
```

**File:** notify/src/lib.rs (L423-440)
```rust
        // notify script
        if let Some(script) = self.config.network_alert_notify_script.clone() {
            let script_timeout = self.timeout.script;
            self.handle.spawn(async move {
                let args = [message];
                match timeout(script_timeout, Command::new(&script).args(&args).status()).await {
                    Ok(ret) => match ret {
                        Ok(status) => {
                            debug!("the network_alert_notify script exited with: {}", status)
                        }
                        Err(e) => error!(
                            "failed to run network_alert_notify_script: {} {}, error: {}",
                            script, args[0], e
                        ),
                    },
                    Err(_) => ckb_logger::warn!("network_alert_notify_script {} timed out", script),
                }
            });
```

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```
