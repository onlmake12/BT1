### Title
Signature Malleability in Network Alert Verification — No Low-S Enforcement Allows Alternate Valid Signatures for the Same Alert (`util/crypto/src/secp/signature.rs`, `util/multisig/src/secp256k1.rs`, `util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert signature verification pipeline (`Signature::is_valid()` → `verify_m_of_n` → `Verifier::verify_signatures`) does not enforce the low-S constraint on ECDSA signatures. Because the secp256k1 curve allows two valid `s` values for any `(r, message)` pair — `s` and `N - s` — an unprivileged P2P peer can take any legitimately broadcast alert, produce a malleable variant with `s' = N - s` and `v' = v XOR 1`, and relay it to other nodes. The malleable variant passes all signature checks. Nodes that receive the malleable form first mark the alert ID as already received and will subsequently ignore the original form, fragmenting alert propagation across the network.

---

### Finding Description

**Root cause — `Signature::is_valid()` accepts high-S values:** [1](#0-0) 

The validity check is:
```
self.v() <= 1 && h_r < N && h_r >= ONE && h_s < N && h_s >= ONE
```

`h_s < N` permits any `s` in `[1, N-1]`. There is no upper bound of `N/2`. For any valid signature `(r, s, v)`, the malleable form `(r, N-s, v^1)` satisfies `N-s ∈ [1, N-1]` and therefore also passes `is_valid()`.

The CHANGELOG explicitly records that the low-S check was intentionally removed in v0.15.0: [2](#0-1) 

**Propagation into alert verification:**

`Verifier::verify_signatures` filters signatures through `is_valid()` before passing them to `verify_m_of_n`: [3](#0-2) 

`verify_m_of_n` uses `sig.recover(message)` to recover the public key: [4](#0-3) 

`Signature::recover` calls `recover_ecdsa` on the raw `RecoverableSignature`: [5](#0-4) 

For a malleable signature `(r, N-s, v^1)`, `recover_ecdsa` returns the **same public key** as for `(r, s, v)`. The malleable form therefore passes both `is_valid()` and `verify_m_of_n`.

**Alert relay deduplication is by alert ID, not by signature bytes:** [6](#0-5) 

Once a node marks an alert ID as received, any subsequent message with the same ID — including the original, legitimately signed form — is silently dropped.

**The raw received bytes are relayed verbatim:** [7](#0-6) 

The `data` variable is the raw bytes from the peer, not re-serialized from the parsed alert. A malleable form injected by an attacker propagates as-is to all connected peers.

---

### Impact Explanation

An unprivileged P2P peer who observes a valid network alert (which is publicly broadcast) can:

1. Compute `s' = N - s` and `v' = v XOR 1` for each signature in the alert.
2. Reconstruct a byte-identical alert body with the malleable signatures.
3. Relay this malleable alert to targeted nodes before the legitimate alert arrives.

Nodes receiving the malleable form first will:
- Accept it (passes `is_valid()` and `verify_m_of_n`)
- Mark the alert ID as received
- Relay the malleable form onward
- Silently drop the original form when it arrives

The alert content (message, ID, expiry) is unchanged, so the information still propagates. However, the network's view of the canonical alert bytes is fragmented: some nodes hold the original form, others hold the malleable form. This undermines the integrity guarantee of the alert system and demonstrates that the signature scheme is non-canonical, which is a direct analog to the reported `ecrecover` malleability class.

---

### Likelihood Explanation

The attack requires only:
- A connection to the CKB P2P network (any unprivileged peer)
- Observation of a valid alert in transit (alerts are broadcast publicly)
- Arithmetic on the `s` component of each signature (trivial computation)

No private keys, no privileged access, and no brute force are required. The attacker does not need to be a miner or hold any CKB. The only timing constraint is injecting the malleable form before the original reaches a target node, which is feasible for a well-connected adversary.

---

### Recommendation

Enforce the low-S constraint in `Signature::is_valid()` by adding a check that `h_s <= N/2` (i.e., `h_s <= 0x7fffffff_ffffffff_ffffffff_ffffffff_5d576e73_57a4501d_dfe92f46_681b20a0`). This is the standard Bitcoin/BIP-146 normalization and is what the `secp256k1` library's `sign_ecdsa` (non-recoverable) path enforces internally. Alternatively, normalize the signature to low-S form upon ingestion using `secp256k1_ecdsa_signature_normalize` before passing it to `verify_m_of_n`.

---

### Proof of Concept

Given a valid alert with signature bytes `[r (32 bytes) | s (32 bytes) | v (1 byte)]`:

```
# Original: (r, s, v)
# Malleable: (r, N - s, v ^ 1)
# where N = 0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_BAAEDCE6_AF48A03B_BFD25E8C_D0364141

malleable_s = N - s          # still in [1, N-1], passes is_valid()
malleable_v = v ^ 1          # flips recovery bit

# Construct malleable alert with same raw/id/message/notice_until fields
# but replace each signature with (r || malleable_s || malleable_v)
```

The malleable alert passes `Signature::is_valid()` (line 77 of `signature.rs`) because `malleable_s < N && malleable_s >= ONE`. It passes `verify_m_of_n` (line 36 of `secp256k1.rs`) because `recover_ecdsa` on `(r, N-s, v^1)` returns the same public key as on `(r, s, v)`. Any node that receives this malleable alert first will accept it, store it, and ignore the original when it arrives — confirmed by the `has_received(alert_id)` early-return at line 147 of `alert_relayer.rs`. [8](#0-7) [1](#0-0) [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** util/crypto/src/secp/signature.rs (L16-17)
```rust
const N: H256 = h256!("0xffffffff_ffffffff_ffffffff_fffffffe_baaedce6_af48a03b_bfd25e8c_d0364141");
const ONE: H256 = h256!("0x1");
```

**File:** util/crypto/src/secp/signature.rs (L63-78)
```rust
    pub fn is_valid(&self) -> bool {
        let h_r = match H256::from_slice(self.r()) {
            Ok(h_r) => h_r,
            Err(_) => {
                return false;
            }
        };

        let h_s = match H256::from_slice(self.s()) {
            Ok(h_s) => h_s,
            Err(_) => {
                return false;
            }
        };
        self.v() <= 1 && h_r < N && h_r >= ONE && h_s < N && h_s >= ONE
    }
```

**File:** util/crypto/src/secp/signature.rs (L90-100)
```rust
    pub fn recover(&self, message: &Message) -> Result<Pubkey, Error> {
        let context = &SECP256K1;
        let recoverable_signature = self.to_recoverable()?;
        let message = SecpMessage::from_digest_slice(message.as_bytes())?;
        let pubkey = context.recover_ecdsa(&message, &recoverable_signature)?;
        let serialized = pubkey.serialize_uncompressed();

        let mut pubkey = [0u8; 64];
        pubkey.copy_from_slice(&serialized[1..65]);
        Ok(pubkey.into())
    }
```

**File:** CHANGELOG.md (L2450-2450)
```markdown
- #938: Remove low S check from util-crypto (@jjyr)
```

**File:** util/network-alert/src/verifier.rs (L36-55)
```rust
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
```

**File:** util/multisig/src/secp256k1.rs (L28-46)
```rust
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
```

**File:** util/network-alert/src/alert_relayer.rs (L144-162)
```rust
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
```

**File:** util/network-alert/src/alert_relayer.rs (L166-178)
```rust
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
