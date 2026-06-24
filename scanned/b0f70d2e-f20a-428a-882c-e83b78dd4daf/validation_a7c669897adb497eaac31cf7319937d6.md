Audit Report

## Title
Network Alert `RawAlert` Hash Missing Chain Identifier Enables Cross-Network Replay of Signed Alerts - (File: `util/network-alert/src/verifier.rs`)

## Summary
The signing hash for CKB network alerts is computed as a plain `blake2b` over `RawAlert` bytes with no chain-specific domain separator. Because the same four developer public keys are hard-coded in a single embedded `alert_signature.toml` used by all CKB networks, a valid alert signed for one network is cryptographically valid on any other. The P2P receive handler performs no expiry check, so an attacker can replay any captured signed alert — including expired ones — directly to mainnet peers, causing the false alert to be accepted, stored, and re-broadcast to the entire mainnet P2P network.

## Finding Description

**Root cause — no domain separator in the signing hash:**

`Verifier::verify_signatures` computes the signing message as:

```rust
// util/network-alert/src/verifier.rs L35
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

`calc_alert_hash` delegates to `calc_hash`, which is `blake2b_256(self.as_slice())` — a plain hash of the raw `RawAlert` molecule bytes with no chain identifier mixed in.

The `RawAlert` molecule schema contains only application-level fields (`notice_until`, `id`, `cancel`, `priority`, `message`, `min_version`, `max_version`) and no `chain_id` or genesis hash field.

**Same keys across all networks:**

`NetworkAlertConfig::default()` embeds `alert_signature.toml` at compile time via `include_bytes!`. This single file defines the 4 production public keys used by every CKB binary regardless of which network it runs on. The `test_alert_20230001` test confirms this by constructing a verifier from `NetworkAlertConfig::default()` and successfully verifying real production signatures against those keys — proving the same key set is active on all networks.

**No expiry check in the P2P receive path:**

`AlertRelayer::received` performs only three checks before accepting and re-broadcasting an alert:
1. UTF-8 validity of string fields
2. `has_received(alert_id)` dedup check
3. `verify_signatures` — signature validity only

There is no `notice_until` check. The expiry check exists exclusively in the RPC `send_alert` handler (`rpc/src/module/alert.rs` L106: `if notice_until < now_ms { return Err(...) }`). An attacker bypassing the RPC and connecting directly via P2P skips this check entirely.

`Notifier::add()` also performs no expiry check when storing and notifying the alert. `clear_expired_alerts` is only called lazily on `connected` events, not on `received`.

**Complete exploit path:**
1. Capture a signed alert from testnet P2P traffic (or use the known 2023 alert bytes, which are accepted by the P2P handler regardless of expiry since no expiry check exists there).
2. Connect to a mainnet node as a normal P2P peer.
3. Send the captured alert bytes via the Alert protocol.
4. The mainnet node calls `verify_signatures`: computes `blake2b(raw_alert.as_slice())` — identical hash to testnet — and verifies 2-of-4 signatures against the same hard-coded mainnet keys. **Pass.**
5. The alert is added to `notifier.received_alerts` and `noticed_alerts`, `notify_controller.notify_network_alert` is called, and the alert is re-broadcast to all connected peers.
6. Every receiving mainnet node repeats steps 4–5, propagating the false alert across the entire network.

## Impact Explanation

The alert system is specifically designed to warn all CKB users about critical bugs and prompt software upgrades. A false developer-signed alert propagated across the entire mainnet P2P network and displayed to all node operators constitutes concrete damage to the CKB economy: it can cause users to halt operations, trigger unnecessary software upgrades, and undermine trust in the network. This maps to **Critical — Vulnerabilities which could easily damage CKB economy**, as the alert mechanism is the primary out-of-band communication channel for critical network events and a false alert exploiting it has direct economic consequences.

## Likelihood Explanation

The attack requires no privileged access. The only precondition is the existence of a signed alert — either a future-dated testnet alert captured from P2P traffic, or any previously signed alert (since the P2P path has no expiry check). The 2023 production alert (`id=20230001`) with known signatures is embedded in the test suite and its bytes are publicly derivable. An attacker needs only to: (a) construct the molecule-encoded `Alert` bytes from the known fields and signatures, and (b) connect to any mainnet node as a P2P peer and send those bytes. No credentials, no mining, no special network position required.

## Recommendation

Include a chain-specific identifier in the alert hash computation. The genesis block hash (available at node startup) should be mixed in before signing and verification:

```rust
// In Verifier::verify_signatures or in calc_alert_hash:
let mut blake2b = new_blake2b();
blake2b.update(genesis_hash.as_slice()); // chain-specific domain separator
blake2b.update(alert.raw().as_slice());
blake2b.finalize(&mut result);
```

Additionally, add an expiry check in `AlertRelayer::received` before accepting and re-broadcasting:

```rust
let now = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now {
    // drop silently or ban peer
    return;
}
```

## Proof of Concept

The `test_alert_20230001` test in `util/network-alert/src/tests/generate_alert_signature.rs` already provides the exact signed alert bytes. To reproduce the cross-network replay:

1. Construct the molecule-encoded `Alert` using the fields and signatures from `test_alert_20230001` (id=20230001, the two hex signatures in lines 74–75).
2. Start a CKB mainnet node with the Alert protocol enabled.
3. Connect to it as a P2P peer and send the encoded alert bytes on the Alert protocol channel.
4. Observe: `verify_signatures` returns `Ok(())` (same keys, same hash), `notifier.add()` is called, `notify_network_alert` fires, and the alert is re-broadcast to all connected peers.
5. Query `get_blockchain_info` on the mainnet node — the false alert appears in the `alerts` field.

The existing unit test `test_alert_20230001` passing against `NetworkAlertConfig::default()` directly proves step 4 succeeds on mainnet keys.