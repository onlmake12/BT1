### Title
Signature Malleability in `Signature::is_valid()` Permits High-S Alert Signatures, Enabling Malleable Alert Relay - (File: `util/crypto/src/secp/signature.rs`)

---

### Summary

`Signature::is_valid()` does not enforce low-S normalization (i.e., `s ≤ N/2`). Any valid network alert signature `(r, s, v)` has a computable malleable twin `(r, N−s, 1−v)` that also passes `is_valid()` and recovers to the same public key. An unprivileged P2P peer who observes a legitimate alert can construct a byte-for-byte different alert carrying only malleable signatures, broadcast it ahead of the original, and cause receiving nodes to permanently store and relay the malleable form — rejecting the canonical original when it arrives later.

---

### Finding Description

**Root cause — `Signature::is_valid()` (line 77):**

```rust
self.v() <= 1 && h_r < N && h_r >= ONE && h_s < N && h_s >= ONE
```

The upper bound on `s` is `s < N`, not `s ≤ N/2`. Both `s` and `N−s` satisfy this range check, so both forms of a signature are accepted as "valid." [1](#0-0) 

**Verification pipeline for network alerts:**

1. `AlertRelayer::received()` first checks `has_received(alert_id)` — deduplication is keyed on the 32-bit `alert_id`, **not** on signature bytes. [2](#0-1) 

2. `Verifier::verify_signatures()` filters each signature through `sig.is_valid()` before passing the list to `verify_m_of_n`. [3](#0-2) 

3. `verify_m_of_n` calls `sig.recover(message)` on each signature. Because `(r, N−s, 1−v)` recovers to the **same** public key as `(r, s, v)`, the malleable variants satisfy the m-of-n threshold check identically. [4](#0-3) 

4. On success the alert is stored in `received_alerts` (keyed by `alert_id`) and broadcast to peers. [5](#0-4) 

**Attack flow:**

1. Attacker connects as a normal P2P peer and observes a legitimate alert carrying valid Nervos Foundation signatures `[(r₁,s₁,v₁), (r₂,s₂,v₂)]`.
2. Attacker computes malleable twins: `(r₁, N−s₁, 1−v₁)` and `(r₂, N−s₂, 1−v₂)`. Both pass `is_valid()` because `N−sᵢ ∈ [1, N)`.
3. Attacker constructs an alert with identical raw content (same `alert_id`, same message) but substitutes the malleable signatures.
4. Attacker relays this malleable alert to nodes that have not yet received the original.
5. Those nodes: pass `is_valid()` → pass `verify_m_of_n` → store the malleable alert under `alert_id` → mark `alert_id` as received → relay the malleable form onward.
6. When the canonical alert subsequently arrives at those nodes, `has_received(alert_id)` returns `true` and it is silently dropped. [6](#0-5) 

---

### Impact Explanation

The alert content (message text, `notice_until`, `cancel` field) is identical in both forms, so the user-visible alert is unaffected. The concrete impact is:

- **Network-wide inconsistency in stored alert bytes**: a partition of nodes stores the malleable form; the rest store the canonical form. Both are keyed by the same `alert_id` and both display the same message.
- **Canonical alert permanently suppressed on affected nodes**: once a node stores the malleable form, the original is rejected forever (no re-processing path exists).
- **Latent risk on logic changes**: if future code ever keys deduplication, cancellation, or any security-sensitive decision on the full alert bytes or signature bytes rather than `alert_id`, the malleability becomes directly exploitable for replay or suppression attacks.

Severity: **Low** — current impact is non-security correctness (byte-level inconsistency, same semantic content).

---

### Likelihood Explanation

- **Attacker prerequisites**: none beyond being a connected P2P peer and having observed one valid alert on the network. Alerts are broadcast to all peers, so observation is trivial.
- **Computation**: flipping `s → N−s` and `v → 1−v` is a single modular subtraction; no cryptographic work required.
- **Timing**: the attacker must relay the malleable form to a target node before the canonical form arrives. On a well-connected network this window is narrow but non-zero, especially for nodes that connect after the alert is first broadcast.

Likelihood: **Low-to-Medium** (trivial to compute, but requires a race against normal propagation).

---

### Recommendation

Enforce low-S normalization inside `Signature::is_valid()`:

```rust
// Replace:
self.v() <= 1 && h_r < N && h_r >= ONE && h_s < N && h_s >= ONE

// With (HALF_N = N/2):
const HALF_N: H256 = h256!("0x7fffffff_ffffffff_ffffffff_ffffffff_5d576e73_57a4501d_dfe92f46_681b20a0");
self.v() <= 1 && h_r < N && h_r >= ONE && h_s <= HALF_N && h_s >= ONE
```

This mirrors the normalization performed by OpenZeppelin's ECDSA library and Bitcoin's `IsLowDERSignature`. It ensures each `(message, key)` pair has exactly one accepted signature form, eliminating the malleable twin entirely. [7](#0-6) 

---

### Proof of Concept

Given a live alert with signatures `[sig_bytes_1, sig_bytes_2]` (65 bytes each, format `r[0..32] || s[32..64] || v[64]`):

```python
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

def malleable(sig_bytes: bytes) -> bytes:
    r = sig_bytes[0:32]
    s = int.from_bytes(sig_bytes[32:64], 'big')
    v = sig_bytes[64]
    s_prime = (N - s) % N
    v_prime = 1 - v          # flip recovery id
    return r + s_prime.to_bytes(32, 'big') + bytes([v_prime])

malleable_sig_1 = malleable(sig_bytes_1)
malleable_sig_2 = malleable(sig_bytes_2)
```

Construct an alert with the same raw fields but `signatures = [malleable_sig_1, malleable_sig_2]`. Send it via the P2P alert protocol to a target node before the canonical alert arrives. The node will:

1. Accept both signatures through `is_valid()` (both `N−s` values are in `[1, N)`). [1](#0-0) 
2. Recover the same public keys via `sig.recover(message)`, satisfying the m-of-n threshold. [8](#0-7) 
3. Store the malleable alert under `alert_id` and reject the canonical form when it arrives. [9](#0-8)

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

**File:** util/network-alert/src/alert_relayer.rs (L144-149)
```rust
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/alert_relayer.rs (L163-178)
```rust
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

**File:** util/network-alert/src/notifier.rs (L93-122)
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

        // check conditions, figure out do we need to notice this alert
        if !self.is_version_effective(alert) {
            debug!("Received a version ineffective alert {:?}", alert);
            return;
        }

        if self.noticed_alerts.contains(alert) {
            return;
        }
        self.notify_controller.notify_network_alert(alert.clone());
        self.noticed_alerts.push(alert.clone());
        // sort by priority
        self.noticed_alerts.sort_by_key(|a| {
            let priority: u32 = a.raw().priority().into();
            u32::MAX - priority
        });
    }
```
