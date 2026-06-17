## Analysis

### Entry Point Accessibility

`actions.move` exposes two `public fun` entry points callable by any Sui address: [1](#0-0) [2](#0-1) 

Both are `public fun` (not `public(package)`), so any unprivileged caller can invoke them with a valid Wormhole VAA.

### The Sequence Guard

The entire replay/ordering defense lives in `process_incoming`: [3](#0-2) 

```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
```

The check is `seen_sequence < sequence` — strictly "greater than last seen" — **not** `seen_sequence + 1 == sequence`. Any sequence number strictly greater than the current `seen_sequence` is accepted, and `seen_sequence` is immediately advanced to that value.

### The Attack Path

1. Pyth governance issues VAA-N (e.g., rotate a trusted signer) and VAA-N+1000 (e.g., update signer expiry). Both are signed by Wormhole guardians and publicly observable on the Wormhole network once signed.
2. An attacker observing the Wormhole network sees both VAAs before either is submitted to Sui.
3. Attacker calls `update_trusted_signer(state, vaa_N_plus_1000)` → passes (`seen_sequence=0 < N+1000`), `seen_sequence` advances to N+1000.
4. Attacker (or anyone) later calls `update_trusted_signer(state, vaa_N)` → aborts with `EOldSequenceNumber` (`N+1000 < N` is false).
5. VAA-N is **permanently unexecutable**. The governance action it encoded (e.g., a signer rotation) can never be applied.

### Why the Preconditions Are Realistic

Wormhole VAAs are public once guardian-signed. Governance batches (multiple VAAs signed in the same governance cycle but submitted to Sui sequentially) create a window where both a low-sequence and a high-sequence VAA are simultaneously observable but unsubmitted. A front-runner monitoring the Wormhole guardian network can reorder submission.

### Conclusion

This is a real, reachable vulnerability. The root cause is the non-consecutive sequence check in `governance::process_incoming`. The attacker is unprivileged (only needs to be a Sui transaction submitter with access to publicly observable Wormhole VAAs). The impact is permanent suppression of specific governance decisions.

---

### Title
Governance VAA sequence check allows out-of-order submission, permanently blocking lower-sequence governance actions — (`lazer/contracts/sui/sources/governance.move`)

### Summary
`governance::process_incoming` accepts any VAA with `sequence > seen_sequence` and advances `seen_sequence` to that value. Submitting a high-sequence VAA first permanently prevents all lower-sequence VAAs from executing, allowing an unprivileged front-runner to suppress specific governance actions (signer rotations, upgrades).

### Finding Description
The sequence guard in `process_incoming` is:

```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
``` [3](#0-2) 

This is a "monotonically increasing" check, not a "strictly consecutive" check. Submitting VAA with sequence N+K (K > 0) before VAA with sequence N advances `seen_sequence` to N+K, making VAA-N permanently invalid. The public entry points `upgrade` and `update_trusted_signer` in `actions.move` accept any caller: [4](#0-3) [5](#0-4) 

The `Governance` struct tracks only a single `seen_sequence` field with no pending-queue or gap-tolerance mechanism: [6](#0-5) 

### Impact Explanation
An attacker can permanently suppress any governance action (trusted signer rotation, contract upgrade) by front-running its submission with a later-sequence VAA. This directly manipulates the effective governance outcome on-chain, even though the suppressed VAA was legitimately voted on and signed by Wormhole guardians.

### Likelihood Explanation
Wormhole VAAs are publicly observable once guardian-signed. Any governance cycle that produces multiple VAAs (common during upgrades or key rotations) creates a window where a front-runner can reorder submission. The attack requires no privileged access, no key compromise, and no Sybil capability — only the ability to submit Sui transactions.

### Recommendation
Replace the `<` check with a strict consecutive check:

```move
assert!(self.seen_sequence + 1 == sequence, EOldSequenceNumber);
```

This ensures governance actions must be applied in exact issuance order, matching the invariant that governance decisions are sequentially dependent.

### Proof of Concept
State-transition test (pseudocode):
```
1. Initialize state with seen_sequence = 0
2. Submit VAA with sequence = 2 → succeeds, seen_sequence = 2
3. Submit VAA with sequence = 1 → aborts with EOldSequenceNumber
4. Assert: governance action encoded in sequence=1 is permanently unexecutable
```
This is directly testable using the existing `state::new_for_test` and `vaa::parse_test_only` test helpers already present in the codebase. [7](#0-6)

### Citations

**File:** lazer/contracts/sui/sources/actions.move (L42-53)
```text
public fun upgrade(state: &mut State, vaa: VAA): UpgradeTicket {
    let current_cap = state.current_cap();
    let (header, mut parser) = state.unwrap_ptgm(&current_cap, vaa);
    assert!(header.is_upgrade_lazer_contract(), ENotUpgradeLazerContract);

    let version = parser.take_u64_be();
    assert!(version == meta::version() + 1, EInvalidUpgradeVersion);

    let digest = parser.take_bytes(32);
    parser.destroy_empty();
    state.authorize_upgrade(&current_cap, digest)
}
```

**File:** lazer/contracts/sui/sources/actions.move (L61-70)
```text
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
    let current_cap = state.current_cap();
    let (header, mut parser) = state.unwrap_ptgm(&current_cap, vaa);
    assert!(header.is_update_trusted_signer(), ENotUpdateTrustedSigner);

    let public_key = parser.take_bytes(secp256k1_compressed_pubkey_len());
    let expires_at = parser.take_u64_be();
    parser.destroy_empty();
    state.update_trusted_signer(&current_cap, public_key, expires_at);
}
```

**File:** lazer/contracts/sui/sources/actions.move (L92-104)
```text
#[test]
public fun test_upgrade() {
    let mut ctx = tx_context::dummy();
    let mut state = state::new_for_test(&mut ctx, test_governance());

    let (_, verifiedVaa) = vaa::parse_test_only(TEST_UPGRADE_VAA);
    let ticket = upgrade(&mut state, verifiedVaa);

    let receipt = ticket.test_upgrade();
    commit_upgrade(&mut state, receipt);

    destroy(state);
}
```

**File:** lazer/contracts/sui/sources/governance.move (L34-38)
```text
public struct Governance has copy, drop, store {
    chain_id: u16,
    address: ExternalAddress,
    seen_sequence: u64,
}
```

**File:** lazer/contracts/sui/sources/governance.move (L63-64)
```text
    assert!(self.seen_sequence < sequence, EOldSequenceNumber);
    self.seen_sequence = sequence;
```
