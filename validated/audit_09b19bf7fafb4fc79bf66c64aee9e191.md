The code is clear. Let me trace the exact path.

**`governance.process_incoming`** (line 63):
```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
```

**`actions.upgrade` and `actions.update_trusted_signer`** are both `public fun` — permissionless submission.

The attack is concrete:
1. Pyth governance broadcasts VAA(N=update_trusted_signer) and VAA(N+1=upgrade) simultaneously on Wormhole.
2. Attacker observes both on the Wormhole network (public) and calls `upgrade(state, VAA(N+1))` first → `seen_sequence = N+1`.
3. Anyone who then calls `update_trusted_signer(state, VAA(N))` hits `assert!(N+1 < N)` → aborts with `EOldSequenceNumber` permanently.

VAA(N) is now unprocessable forever — there is no recovery path.

---

### Title
Permissionless Out-of-Order VAA Submission Permanently Blocks Lower-Sequence Governance Actions — (`lazer/contracts/sui/sources/governance.move`)

### Summary
The `governance::process_incoming` function enforces a strict monotone sequence check (`seen_sequence < sequence`) and advances `seen_sequence` on every successful call. Because `actions::upgrade` and `actions::update_trusted_signer` are both `public fun` (permissionless), any observer can submit a higher-sequence VAA before a lower-sequence one, permanently rendering the lower-sequence VAA unprocessable.

### Finding Description
`process_incoming` in `governance.move` checks:

```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
``` [1](#0-0) 

Once `seen_sequence` is set to N+1, any VAA with sequence ≤ N will permanently fail this assertion. There is no mechanism to process skipped sequences.

The submission entry points are both unconditionally public:

```move
public fun upgrade(state: &mut State, vaa: VAA): UpgradeTicket { ... }
public fun update_trusted_signer(state: &mut State, vaa: VAA) { ... }
``` [2](#0-1) [3](#0-2) 

Any address can call either function with any valid Wormhole-signed VAA it has observed.

### Impact Explanation
If VAA(N) is a signer rotation (`update_trusted_signer`) and it is permanently blocked:
- The existing trusted signer's `expires_at` cannot be updated or a new signer cannot be added.
- Once the current signer expires, `verify_le_ecdsa_message` will abort with `ESignerExpired` for every price update. [4](#0-3) 
- This causes a **total, permanent DoS on all Lazer price updates** on Sui until a contract upgrade is performed — itself also potentially blockable by the same attack.

### Likelihood Explanation
- Wormhole VAAs are public once broadcast; no privileged access is needed.
- The attacker only needs to observe two simultaneously-broadcast VAAs and submit the higher-sequence one first.
- On Sui, transactions touching the same shared `State` object are serialized, so the attacker simply needs their transaction to be ordered before the legitimate one — achievable by submitting with a higher gas price or by racing the mempool.
- No key compromise, oracle manipulation, or Sybil attack is required.

### Recommendation
Replace the strict less-than check with a `>=` (already-seen) guard that rejects only exact replays or already-consumed sequences, and process each sequence exactly once regardless of submission order. Alternatively, use a `VecSet<u64>` of consumed sequences instead of a high-water mark, so out-of-order delivery is tolerated:

```move
// Instead of:
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;

// Use a consumed-set approach:
assert!(!self.consumed_sequences.contains(&sequence), EAlreadyConsumed);
self.consumed_sequences.insert(sequence);
```

### Proof of Concept
```
State: seen_sequence = 0

Step 1: attacker calls upgrade(state, VAA{sequence=2})
  → assert!(0 < 2) passes
  → seen_sequence = 2

Step 2: legitimate relayer calls update_trusted_signer(state, VAA{sequence=1})
  → assert!(2 < 1) FAILS → abort EOldSequenceNumber

VAA(sequence=1) is now permanently unprocessable.
If VAA(1) was a signer rotation, the trusted signer expires → all price updates revert ESignerExpired.
``` [5](#0-4) [6](#0-5)

### Citations

**File:** lazer/contracts/sui/sources/governance.move (L55-65)
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
```

**File:** lazer/contracts/sui/sources/actions.move (L42-42)
```text
public fun upgrade(state: &mut State, vaa: VAA): UpgradeTicket {
```

**File:** lazer/contracts/sui/sources/actions.move (L61-61)
```text
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
```

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L63-63)
```text
    assert!(clock.timestamp_ms() < expires_at_ms, ESignerExpired);
```

**File:** lazer/contracts/sui/sources/state.move (L41-46)
```text
public struct State has key {
    id: UID,
    trusted_signers: vector<TrustedSignerInfo>,
    upgrade_cap: UpgradeCap,
    governance: Governance,
}
```
