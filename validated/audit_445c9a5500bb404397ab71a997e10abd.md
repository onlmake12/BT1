### Title
Network Alert Signature Lacks Chain-Specific Domain Separation, Enabling Cross-Network Replay — (`File: util/network-alert/src/verifier.rs`, `util/gen-types/src/extension/calc_hash.rs`)

---

### Summary

The CKB Network Alert system signs and verifies alert messages using a hash of the `RawAlert` struct that contains no chain-specific identifier (no genesis hash, no network name, no chain ID). Because the same Nervos Foundation key holders and the same hardcoded public keys are used across all CKB networks (mainnet, testnet, devnet), a validly-signed alert from one network can be replayed verbatim on any other network and will pass signature verification, causing it to be accepted and broadcast to all peers on that network.

---

### Finding Description

The `RawAlert` molecule struct is defined as:

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

None of these fields are network-specific. The alert hash used as the signing message is computed by `calc_alert_hash()`, which delegates to the generic `CalcHash` trait:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // → blake2b_256(self.as_slice())
}
``` [2](#0-1) 

The generic `CalcHash` implementation is a plain `blake2b_256` over the raw bytes with no domain tag, no genesis hash, and no network name mixed in:

```rust
fn calc_hash(&self) -> packed::Byte32 {
    blake2b_256(self.as_slice()).into()
}
``` [3](#0-2) 

The verifier in `util/network-alert/src/verifier.rs` uses this hash directly as the ECDSA message:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [4](#0-3) 

It then checks the signatures against the configured public keys:

```rust
verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [5](#0-4) 

The P2P `AlertRelayer` accepts alerts from any connected peer, verifies them with the same `Verifier`, and if they pass, broadcasts them to all other peers:

```rust
if let Err(err) = self.verifier.verify_signatures(&alert) { ... ban_peer ... }
// mark sender as known
// broadcast message
self.notifier.lock().add(&alert);
``` [6](#0-5) 

The `send_alert` RPC endpoint also accepts alerts from any local RPC caller and broadcasts them after the same signature check:

```rust
fn send_alert(&self, alert: Alert) -> Result<()> {
    ...
    let result = self.verifier.verify_signatures(&alert);
    match result {
        Ok(()) => { self.notifier.lock().add(&alert); self.network_controller.broadcast_with_handle(...) }
``` [7](#0-6) 

Because the signed message is identical for the same `RawAlert` content regardless of which CKB network it was originally intended for, a signature valid on testnet is also valid on mainnet and vice versa.

---

### Impact Explanation

An attacker who observes a legitimately signed alert on CKB testnet (or any other CKB-based network sharing the same alert public keys) can replay that exact `Alert` message — raw bytes plus signatures — on CKB mainnet. The replayed alert will:

1. Pass `verify_signatures()` because the signed hash is identical (no network discriminator).
2. Be accepted by the local node's notifier and displayed to the node operator.
3. Be broadcast to all connected peers via `quick_filter_broadcast`, propagating across the entire mainnet P2P network.

The result is that all mainnet nodes display a false or misleading alert message (e.g., a testnet-specific upgrade warning, a cancelled alert, or a stale version-range warning). This is a network-wide misinformation/griefing vector. No funds are at risk, but operator trust in the alert system is undermined and operators may take unnecessary actions (halting nodes, emergency upgrades) based on a fabricated alert.

---

### Likelihood Explanation

The Nervos Foundation uses the same set of private keys to sign alerts for all CKB networks (the public keys are hardcoded in the default `NetworkAlertConfig`). Alert 20230001 was a real, publicly broadcast alert with known signatures embedded in the test suite:

```rust
"8dca283684ff3cd024bd6a67efb24617e90e31dc69ac809ac6ac5e243a57b7aa...",
"4554b37824e17ea02432507e372c869301a415bf718e0a5a33b6df75cd32fbab..."
``` [8](#0-7) 

Any past or future alert signed for testnet is immediately replayable on mainnet by any unprivileged party who can reach the `send_alert` RPC endpoint or connect to a mainnet peer. The attacker needs no private keys — only the already-public signed alert bytes. The entry path is fully reachable without privilege: the `send_alert` RPC is available to any local caller, and the P2P `AlertRelayer` accepts messages from any connected peer.

---

### Recommendation

Mix a network-specific domain separator into the alert hash before signing. The most natural choice is the genesis block hash, which is already available in `Consensus::genesis_hash()` and uniquely identifies each CKB network. For example, `calc_alert_hash` should commit to:

```
blake2b_256(genesis_hash || raw_alert_bytes)
```

Alternatively, add a `chain_id` or `network_name` field to `RawAlert` itself so the signed content is inherently network-specific, analogous to how the Futureswap fix included the `Registry` contract address in the signed struct.

---

### Proof of Concept

1. Obtain the bytes of any previously broadcast, validly-signed CKB testnet alert (e.g., alert 20230001 with its two known signatures from the test suite).
2. Submit those exact bytes to a CKB **mainnet** node via the `send_alert` RPC:

```json
{
  "jsonrpc": "2.0",
  "method": "send_alert",
  "params": [{
    "id": "0x13a4b1",
    "cancel": "0x0",
    "priority": "0x14",
    "message": "CKB v0.105.* have bugs. Please upgrade to the latest version.",
    "notice_until": "0x187a0c7c3800",
    "min_version": "0.105.0-pre",
    "max_version": "0.105.1",
    "signatures": [
      "0x8dca283684ff3cd024bd6a67efb24617e90e31dc69ac809ac6ac5e243a57b7aa6711228dfbd8a5cc89a68d3065b685e5c56c70740e8d3487fd538dc914d0c97c00",
      "0x4554b37824e17ea02432507e372c869301a415bf718e0a5a33b6df75cd32fbab7cf8176ca8b079c28266ce1f33c3f61fbff19e27be2a85f5a14faa2b1b474e0a01"
    ]
  }],
  "id": 1
}
```

3. `Verifier::verify_signatures()` computes `blake2b_256(raw_alert_bytes)` — identical to the testnet hash — and the 2-of-4 threshold passes.
4. The alert is stored in the notifier and broadcast to all connected mainnet peers via `quick_filter_broadcast`.
5. All reachable mainnet nodes display the stale/misleading testnet alert message.

The root cause is confirmed at:
- `util/gen-types/src/extension/calc_hash.rs` lines 15–21 (`calc_hash` = plain `blake2b_256` with no domain separator) [9](#0-8) 
- `util/gen-types/src/extension/calc_hash.rs` lines 292–299 (`calc_alert_hash` delegates to `calc_hash`) [2](#0-1) 
- `util/network-alert/src/verifier.rs` lines 33–35 (message = `calc_alert_hash()` with no network context) [4](#0-3) 
- `util/network-alert/src/alert_relayer.rs` lines 150–178 (P2P entry path accepts and rebroadcasts) [6](#0-5)

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

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

**File:** util/network-alert/src/verifier.rs (L56-62)
```rust
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
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

**File:** rpc/src/module/alert.rs (L102-124)
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
```

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L73-82)
```rust
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
```
