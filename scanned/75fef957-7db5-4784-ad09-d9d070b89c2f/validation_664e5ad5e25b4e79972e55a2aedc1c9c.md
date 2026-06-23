### Title
Zero `signatures_threshold` in `NetworkAlertConfig` Bypasses Multi-Signature Verification, Allowing Unauthenticated Alert Broadcast — (File: `util/multisig/src/secp256k1.rs`)

---

### Summary

`verify_m_of_n` in `util/multisig/src/secp256k1.rs` contains no guard against `m_threshold == 0`. When `NetworkAlertConfig.signatures_threshold` is set to `0`, every guard in `verify_m_of_n` trivially passes and the function returns `Ok(())` with zero signatures verified. Any RPC caller can then invoke `send_alert` with an unsigned alert and have it accepted and broadcast to the entire P2P network.

---

### Finding Description

**Root cause — `verify_m_of_n` with `m_threshold = 0`** [1](#0-0) 

Three sequential guards all evaluate to `false` when `m_threshold = 0` and `sigs = []`:

| Line | Condition | Evaluation |
|------|-----------|------------|
| 23 | `if m_threshold > sigs.len()` | `0 > 0` → **false** |
| 47 | `if verified_sig_count < m_threshold` | `0 < 0` → **false** |

Both are `usize` comparisons; neither can underflow. The function falls through to `Ok(())` unconditionally.

**No validation of `signatures_threshold` in config or constructor**

`NetworkAlertConfig` declares `signatures_threshold: usize` with no minimum-value constraint: [2](#0-1) 

`Verifier::new()` constructs the verifier and stores the config without checking that `signatures_threshold > 0`: [3](#0-2) 

`verify_signatures` passes `self.config.signatures_threshold` directly to `verify_m_of_n`: [4](#0-3) 

**Attacker-reachable entry point — `send_alert` RPC**

`AlertRpcImpl::send_alert` calls `self.verifier.verify_signatures(&alert)` and, on `Ok(())`, immediately broadcasts the alert to all P2P peers: [5](#0-4) 

---

### Impact Explanation

If a node operator sets `signatures_threshold = 0` in `ckb.toml` (the field is freely configurable with no enforcement of a minimum), any unprivileged RPC caller can:

1. Submit a `send_alert` JSON-RPC call with an empty `signatures` array and any alert content.
2. `verify_m_of_n` returns `Ok(())` with zero signatures verified.
3. The alert is stored in the local notifier and broadcast via `network_controller.broadcast_with_handle` to every connected peer.
4. Receiving peers relay the alert further, propagating it across the entire CKB P2P network.

This entirely bypasses the multi-signature authorization that is the sole security gate for the network alert system. The alert system is documented as requiring 2-of-4 Nervos Foundation signatures; a zero threshold silently removes that requirement. Impact: unauthorized network-wide alert injection, potential user confusion, and disruption of the alert trust model.

---

### Likelihood Explanation

The default embedded config (`alert_signature.toml`) sets `signatures_threshold = 2`, so production nodes using the default are not immediately affected. However:

- The field is a plain `usize` with no documented minimum, no compile-time or runtime enforcement, and no panic/error on zero.
- Any operator who overrides the alert config section in `ckb.toml` with `signatures_threshold = 0` (e.g., for local testing or misconfiguration) silently disables all signature checks.
- No warning is emitted by `Verifier::new()` or anywhere in the startup path when `signatures_threshold = 0`.

This is the direct analog of the original report: a zero value in the authorization parameter causes trivial bypass, and the codebase provides no defense-in-depth guard at the verification layer itself.

---

### Recommendation

1. **Guard in `verify_m_of_n`**: Add an explicit check at the top of the function:
   ```rust
   if m_threshold == 0 {
       return Err(ErrorKind::Threshold { pass_sigs: 0, threshold: 0 }.into());
   }
   ``` [6](#0-5) 

2. **Guard in `Verifier::new()`**: Panic or return an error if `signatures_threshold == 0`:
   ```rust
   assert!(config.signatures_threshold > 0, "signatures_threshold must be > 0");
   ``` [7](#0-6) 

3. **Guard in `NetworkAlertConfig` deserialization**: Use a custom deserializer or `#[validate]` attribute to reject `signatures_threshold = 0` at config-load time. [8](#0-7) 

---

### Proof of Concept

```
# ckb.toml (attacker-controlled node or misconfigured node)
[network_alert]
signatures_threshold = 0
public_keys = []

# RPC call — no signatures required
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "send_alert",
    "params": [{
      "id": "0x1",
      "cancel": "0x0",
      "priority": "0x1",
      "message": "FAKE CRITICAL ALERT",
      "notice_until": "0x999999999999",
      "signatures": []
    }],
    "id": 1
  }'
```

**Trace**:
1. `send_alert` → `verify_signatures` → `verify_m_of_n(msg, 0, [], {})` [4](#0-3) 
2. `m_threshold = 0`: guard at line 23 (`0 > 0`) → false; guard at line 47 (`0 < 0`) → false → `Ok(())` [9](#0-8) 
3. Alert stored and broadcast to all P2P peers. [10](#0-9)

### Citations

**File:** util/multisig/src/secp256k1.rs (L11-54)
```rust
pub fn verify_m_of_n<S>(
    message: &Message,
    m_threshold: usize,
    sigs: &[Signature],
    pks: &HashSet<Pubkey, S>,
) -> Result<(), Error>
where
    S: BuildHasher,
{
    if sigs.len() > pks.len() {
        return Err(ErrorKind::SigCountOverflow.into());
    }
    if m_threshold > sigs.len() {
        return Err(ErrorKind::SigNotEnough.into());
    }

    let mut used_pks: HashSet<Pubkey> = HashSet::with_capacity(m_threshold);
    let verified_sig_count = sigs
        .iter()
        .filter_map(|sig| {
            trace!(
                "Recover sig {:x?} with message {:x?}",
                &sig.serialize()[..],
                message.as_ref()
            );
            match sig.recover(message) {
                Ok(pubkey) => Some(pubkey),
                Err(err) => {
                    debug!("recover secp256k1 sig error: {}", err);
                    None
                }
            }
        })
        .filter(|rec_pk| pks.contains(rec_pk) && used_pks.insert(rec_pk.to_owned()))
        .take(m_threshold)
        .count();
    if verified_sig_count < m_threshold {
        return Err(ErrorKind::Threshold {
            pass_sigs: verified_sig_count,
            threshold: m_threshold,
        }
        .into());
    }
    Ok(())
```

**File:** util/app-config/src/configs/network_alert.rs (L5-12)
```rust
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// The minimum number of required signatures to send a network alert.
    pub signatures_threshold: usize,
    /// The public keys of all the network alert signers.
    pub public_keys: Vec<JsonBytes>,
}
```

**File:** util/network-alert/src/verifier.rs (L22-30)
```rust
    pub fn new(config: NetworkAlertConfig) -> Self {
        let pubkeys = config
            .public_keys
            .iter()
            .map(|raw| Pubkey::from_slice(raw.as_bytes()))
            .collect::<Result<HashSet<Pubkey>, _>>()
            .expect("builtin pubkeys");
        Verifier { config, pubkeys }
    }
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
