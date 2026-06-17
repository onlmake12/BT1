Looking at the exact code path:

**`governance::process_incoming`** (governance.move line 63-64):
```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
```

**`actions::update_trusted_signer`** (actions.move line 61):
```move
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
```

**`state::unwrap_ptgm`** (state.move line 74):
```move
let payload = self.governance.process_incoming(vaa);
```

The check is strictly `seen_sequence < sequence` — not a consecutive check. Any sequence strictly greater than the last seen is accepted, and `seen_sequence` is immediately updated to the new value. The entry point `update_trusted_signer` is `public fun` with no caller restriction. Wormhole VAAs are publicly observable. An attacker can submit a legitimate high-sequence VAA before lower-sequence ones, permanently blocking them.

However, the critical constraint is: **the attacker cannot forge a VAA**. They can only relay a legitimately Wormhole-guardian-signed VAA out of order. This means the governance system must have already produced and signed multiple VAAs that haven't been relayed yet. In normal operation, a trusted relayer submits VAAs in sequence order. The attacker's window is the gap between VAA signing (observable on Wormhole) and on-chain submission.

This is a real, concrete, reachable path with no privilege requirement beyond observing Wormhole VAAs (which are public).

---

### Title
Governance Sequence Monotonicity Allows Out-of-Order VAA Submission to Permanently Block Lower-Sequence Governance Actions — (`lazer/contracts/sui/sources/governance.move`)

### Summary
`governance::process_incoming` accepts any VAA with `sequence > seen_sequence` and immediately advances `seen_sequence` to that value. Because `actions::update_trusted_signer` is an unrestricted `public fun`, any party can submit a valid high-sequence governance VAA before lower-sequence ones are relayed, permanently invalidating all pending lower-sequence VAAs.

### Finding Description
In `governance::process_incoming`, the only ordering invariant enforced is:

```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
``` [1](#0-0) 

This is a monotonicity check, not a consecutiveness check. Submitting sequence N advances `seen_sequence` to N, permanently rejecting all sequences ≤ N. The public entry point:

```move
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
``` [2](#0-1) 

...has no caller restriction. The same applies to `actions::upgrade`: [3](#0-2) 

Both call `state::unwrap_ptgm`, which calls `governance::process_incoming`: [4](#0-3) 

Wormhole VAAs are publicly observable once signed by guardians. An attacker monitoring the Wormhole network can observe a high-sequence governance VAA (e.g., a signer rotation at sequence=1000) and submit it on-chain before the legitimate relayer submits lower-sequence VAAs (e.g., signer additions at sequences 1–999). After sequence=1000 is processed, `seen_sequence=1000`, and all sequences 1–999 are permanently rejected with `EOldSequenceNumber`.

### Impact Explanation
The `trusted_signers` set in `State` is the sole source of authority for verifying Lazer price updates: [5](#0-4) 

If pending signer-addition VAAs (sequences 1–999) are permanently blocked, new signers can never be added. When existing signers expire (each `TrustedSignerInfo` has an `expires_at`), all calls to `parse_and_verify_le_ecdsa_update_v2` will revert with `ESignerNotTrusted` or `ESignerExpired`, halting all price verification and freezing any protocol that depends on Lazer price feeds for fund flows. [6](#0-5) 

### Likelihood Explanation
- Wormhole VAAs are publicly observable before on-chain submission.
- `update_trusted_signer` and `upgrade` are unrestricted `public fun` — no role check, no capability requirement.
- On Sui, transaction ordering is determined by validators; an attacker can submit with competitive gas to front-run the relayer.
- The attack requires only one high-sequence VAA to be observable before its lower-sequence predecessors are submitted — a realistic window during any governance batch operation.

### Recommendation
Replace the monotonicity-only check with a consecutiveness check:

```move
assert!(self.seen_sequence + 1 == sequence, ESequenceNotConsecutive);
self.seen_sequence = sequence;
```

Alternatively, if non-consecutive sequences are intentional (e.g., some sequences are skipped), enforce a strict upper-bound gap (e.g., `sequence <= seen_sequence + MAX_GAP`) to prevent large jumps that would block many pending VAAs.

### Proof of Concept
State-transition test (pseudocode):
```
1. Initialize State with seen_sequence = 0
2. Submit VAA with sequence = 1000 (signer rotation) via update_trusted_signer
   → seen_sequence becomes 1000, signer rotation applied
3. Attempt to submit VAA with sequence = 1 (signer addition)
   → assert!(0 < 1) would pass, but now assert!(1000 < 1) FAILS → EOldSequenceNumber
4. Assert: trusted_signers does not contain the signer from sequence=1
5. Let all existing signers expire (set clock past expires_at)
6. Call parse_and_verify_le_ecdsa_update_v2 → reverts ESignerNotTrusted
``` [7](#0-6)

### Citations

**File:** lazer/contracts/sui/sources/governance.move (L55-66)
```text
public(package) fun process_incoming(
    self: &mut Governance,
    vaa: VAA
): vector<u8> {
    let sequence = vaa.sequence();
    let (chain_id, address, payload) = vaa.take_emitter_info_and_payload();
    assert!(self.chain_id == chain_id, EMismatchedEmitterChainID);
    assert!(self.address == address, EMismatchedAddress);
    assert!(self.seen_sequence < sequence, EOldSequenceNumber);
    self.seen_sequence = sequence;
    payload
}
```

**File:** lazer/contracts/sui/sources/actions.move (L42-42)
```text
public fun upgrade(state: &mut State, vaa: VAA): UpgradeTicket {
```

**File:** lazer/contracts/sui/sources/actions.move (L61-61)
```text
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
```

**File:** lazer/contracts/sui/sources/state.move (L69-74)
```text
public(package) fun unwrap_ptgm(
    self: &mut State,
    _: &CurrentCap,
    vaa: VAA
): (GovernanceHeader, Parser) {
    let payload = self.governance.process_incoming(vaa);
```

**File:** lazer/contracts/sui/sources/state.move (L159-162)
```text
public struct TrustedSignerInfo has copy, drop, store {
    public_key: vector<u8>,
    expires_at: u64,
}
```

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L55-63)
```text
    let trusted_signers = state.trusted_signers(&current_cap);
    let mut maybe_idx = trusted_signers.find_index!(|signer|
        signer.public_key() == &pubkey
    );

    assert!(maybe_idx.is_some(), ESignerNotTrusted);
    let idx = maybe_idx.extract();
    let expires_at_ms = trusted_signers[idx].expires_at_ms();
    assert!(clock.timestamp_ms() < expires_at_ms, ESignerExpired);
```
