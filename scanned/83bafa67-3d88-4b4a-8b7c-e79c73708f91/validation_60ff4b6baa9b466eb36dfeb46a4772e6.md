### Title
Cross-Network Alert Replay via Missing Chain Identifier in `RawAlert` Hash — (`util/gen-types/src/extension/calc_hash.rs`, `util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network-alert system signs and verifies `RawAlert` messages using a multi-signature scheme controlled by the Nervos Foundation. The hash committed to by signers (`calc_alert_hash`) is computed solely from the `RawAlert` molecule-encoded bytes, which contain no chain or network identifier. Because the same signing key set is used across CKB networks (mainnet, testnet), a valid alert signature produced for one network is cryptographically indistinguishable from a valid alert on any other network. An unprivileged P2P peer or RPC caller can replay a mainnet alert verbatim onto testnet nodes (or vice versa), and every node will accept it as authentic.

---

### Finding Description

**Root cause — missing domain separator in `calc_alert_hash`**

The `RawAlert` molecule table is defined as:

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

There is no `network_id`, `genesis_hash`, or any chain-specific field.

The hash that signers commit to is computed in `calc_hash.rs`:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
``` [2](#0-1) 

`calc_hash()` is the blanket `blake2b_256(self.as_slice())` implementation — it hashes only the raw molecule bytes of `RawAlert` with no additional context. [3](#0-2) 

**Verification path — no chain check**

`Verifier::verify_signatures` derives the message to verify from `alert.calc_alert_hash()` and checks it against the configured public keys:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
``` [4](#0-3) 

There is no step that checks whether the alert was intended for this network. The verifier only checks that enough valid signatures exist over the hash of the `RawAlert` content.

**Relay path — alerts propagate via P2P**

Alerts are relayed peer-to-peer across the network. Any connected peer can inject an alert message into the gossip layer. The `send_alert` RPC endpoint also accepts alerts from any caller when the `Alert` RPC module is enabled. [5](#0-4) 

---

### Impact Explanation

Because `calc_alert_hash` produces an identical digest for the same `RawAlert` content regardless of which CKB network the node is running on, and because the Nervos Foundation uses the same signing key set across networks (evidenced by `NetworkAlertConfig::default()` carrying hardcoded public keys used in both mainnet and testnet contexts):

1. A valid mainnet alert (with real Foundation signatures, publicly observable on the mainnet P2P layer) can be submitted to testnet nodes via P2P relay or the `send_alert` RPC.
2. Every testnet node will call `verify_signatures`, compute the same hash, verify the same signatures against the same public keys, and accept the alert as authentic.
3. The alert is then stored, displayed to operators, and re-relayed to all testnet peers.

Concrete consequences:
- Testnet operators receive mainnet-specific emergency alerts (e.g., "upgrade immediately", "stop mining") and may act on them unnecessarily, disrupting testnet operations.
- Conversely, a testnet alert (if the Foundation ever issues one) could be replayed on mainnet, causing mainnet operators to act on testnet-scoped guidance.
- An attacker who can observe any historical mainnet alert can permanently inject it into testnet at will, with no ability for nodes to distinguish or reject it.

---

### Likelihood Explanation

- **Attacker preconditions**: None beyond being a P2P peer on the target network or having RPC access to a node with the Alert module enabled. Both are reachable by any unprivileged actor.
- **Trigger**: Mainnet alerts are publicly observable on the P2P network. Any attacker can capture a valid signed alert and relay it to testnet peers.
- **No cryptographic break required**: The signatures are already valid; the attacker only replays existing data.
- **Likelihood**: High. The attack requires no special capability beyond network connectivity.

---

### Recommendation

Include a chain-specific domain separator in the data committed to by alert signers. The genesis hash is the canonical CKB network identifier and is already available via `Consensus::genesis_hash()`. The fix should be applied at the hash construction level:

```rust
// In calc_alert_hash, include genesis_hash as a domain separator:
pub fn calc_alert_hash(&self, genesis_hash: &packed::Byte32) -> packed::Byte32 {
    let mut hasher = new_blake2b();
    hasher.update(genesis_hash.as_slice());
    hasher.update(self.as_slice());
    let mut result = [0u8; 32];
    hasher.finalize(&mut result);
    result.into()
}
```

The `genesis_hash` must be threaded through to `Verifier::verify_signatures` and to all signing tooling. This mirrors the standard EIP-155 pattern of including `chain_id` in the signed digest. [6](#0-5) 

---

### Proof of Concept

1. Run a CKB mainnet node and a CKB testnet node.
2. On mainnet, observe any live alert via `get_blockchain_info` RPC or P2P capture. Record the full `Alert` molecule bytes (including `raw` and `signatures`).
3. On testnet, call `send_alert` RPC with the captured mainnet alert bytes (requires Alert RPC module enabled, which is a supported configuration).
4. Observe that the testnet node accepts the alert: `verify_signatures` returns `Ok(())` because `calc_alert_hash` produces the same digest and the Foundation's public keys are identical.
5. Confirm via `get_blockchain_info` on testnet that the mainnet alert now appears as an active alert on testnet nodes, and is being re-relayed to all connected testnet peers.

The root cause is confirmed at:
- `util/gen-types/schemas/extensions.mol` lines 445–453 (no network field in `RawAlert`)
- `util/gen-types/src/extension/calc_hash.rs` lines 292–299 (`calc_alert_hash` = bare `blake2b_256` of molecule bytes)
- `util/network-alert/src/verifier.rs` lines 33–62 (no chain check in verification)

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

**File:** util/network-alert/src/verifier.rs (L33-62)
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
```

**File:** test/src/specs/alert/alert_propagation.rs (L36-58)
```rust
    fn run(&self, nodes: &mut Vec<Node>) {
        out_ibd_mode(nodes);
        connect_all(nodes);

        let node0 = &nodes[0];
        let notice_until = ckb_systemtime::unix_time_as_millis() + 100_000;

        // create and relay alert
        let id1: u32 = 42;
        let warning1: Bytes = b"pretend we are in dangerous status".to_vec().into();
        let raw_alert = RawAlert::new_builder()
            .id(id1)
            .message(&warning1)
            .notice_until(notice_until)
            .build();
        let alert = create_alert(raw_alert, &self.privkeys);
        node0.rpc_client().send_alert(alert.clone().into());
        let ret = wait_until(20, || {
            nodes
                .iter()
                .all(|node| !node.rpc_client().get_blockchain_info().alerts.is_empty())
        });
        assert!(ret, "Alert should be relayed, but not");
```

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```
