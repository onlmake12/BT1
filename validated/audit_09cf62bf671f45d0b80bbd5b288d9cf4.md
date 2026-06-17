### Title
`block.prevrandao` and `block.timestamp` Used as User Contribution Seed in Entropy Commit-Reveal Scheme — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The internal `random()` function in `Entropy.sol` derives the user's secret contribution from `block.prevrandao`, `block.timestamp`, `msg.sender`, and a stored seed. Three public `requestV2()` overloads call this function on behalf of users who do not supply their own randomness. Because `block.prevrandao` and `block.timestamp` are validator-influenceable, the user contribution is not truly unpredictable, collapsing the two-party security guarantee of the Entropy protocol down to a single party (the provider).

---

### Finding Description

The Entropy protocol's security rests on the property that the final random number `r = hash(x_user, x_provider)` is unbiasable as long as **either** the user **or** the provider contributes honestly. The user's contribution is supposed to be a secret value chosen before the provider's contribution is known.

The `random()` function at lines 1079–1089 of `Entropy.sol` generates the user contribution as:

```solidity
function random() internal returns (bytes32) {
    _state.seed = keccak256(
        abi.encodePacked(
            block.timestamp,
            block.prevrandao,
            msg.sender,
            _state.seed
        )
    );
    return _state.seed;
}
```

This output is used directly as the `userContribution` in three public entry points:

```solidity
// Line 292
assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);

// Line 299-301
assignedSequenceNumber = requestV2(getDefaultProvider(), random(), gasLimit);

// Line 309
assignedSequenceNumber = requestV2(provider, random(), gasLimit);
```

Per [EIP-4399](https://eips.ethereum.org/EIPS/eip-4399), `PREVRANDAO` gives every PoS block proposer 1 bit of influence per slot — they can choose to skip proposing a block to prevent the RANDAO mix from updating. `block.timestamp` is similarly influenceable within a slot's tolerance window. A validator who is also the block proposer for the block containing a `requestV2()` call can therefore bias `random()`'s output.

Because the provider's hash chain is a deterministic sequence (`x_i = hash(x_{i+1})`), anyone who has obtained the provider's hash chain (which the protocol design explicitly acknowledges as a trust assumption) can compute `x_provider` for the upcoming sequence number. Combined with the ability to bias `block.prevrandao`, a colluding validator+provider (or a validator who has obtained the hash chain) can:

1. Compute `x_provider` for the next sequence number off-chain.
2. Enumerate candidate `block.prevrandao` values (only 1 bit of influence, but sufficient to select between two outcomes).
3. Choose the block that produces a favorable `r = hash(random(), x_provider)`.
4. Include the victim's `requestV2()` transaction in that block.

The protocol design documentation itself acknowledges: *"Providers are trusted not to front-run user transactions (via the mempool or colluding with the validator)."* This finding shows that even without provider collusion, a validator who independently obtains the hash chain can exploit the weak user contribution.

---

### Impact Explanation

- Users who call the no-contribution `requestV2()` overloads receive a random number whose user-side entropy is validator-influenceable.
- The two-party security guarantee ("random as long as either party is honest") is broken for these callers: the user's contribution is no longer independently random, so the entire security burden falls on the provider.
- Any application built on top of Entropy that uses these convenience overloads (e.g., NFT mints, lotteries, games) is exposed to outcome manipulation by a colluding validator+provider or a validator with access to the provider's hash chain.
- The `_state.seed` carries over across calls, so a validator who manipulates one call also biases the seed for all subsequent calls to `random()` in the same or future blocks.

---

### Likelihood Explanation

- The three affected `requestV2()` overloads are the **default convenience API** — they are the simplest entry points and are likely to be used by the majority of integrators who follow the documentation's "requestV2()" examples.
- PoS validators have a known, documented 1-bit influence over `block.prevrandao` per EIP-4399 with zero on-chain cost (only opportunity cost of skipping a slot).
- The provider's hash chain is a known trust assumption; a validator who is also the default provider (Fortuna) or who has obtained the chain has full knowledge of `x_provider`.
- No special permissions are required: any validator processing the block can perform this manipulation.

---

### Recommendation

1. **Remove `block.prevrandao` and `block.timestamp` from `random()`**. These are not reliable entropy sources for security-critical operations.
2. **Require callers to always supply their own `userContribution`** (as the `requestV2(address, bytes32, uint32)` overload already does). Remove or deprecate the convenience overloads that call `random()` internally.
3. If on-chain PRNG is required for UX reasons, document clearly that it weakens the security model and that the result is only as secure as the provider, not the two-party guarantee.
4. Alternatively, use a VDF or Chainlink VRF as the user-side contribution source if the caller cannot supply one.

---

### Proof of Concept

1. Attacker is a PoS validator and has obtained the Fortuna provider's hash chain (or is the provider).
2. A victim calls `requestV2()` (no arguments) — the contract calls `random()` internally.
3. The validator computes `x_provider` for the next sequence number from the hash chain.
4. The validator enumerates the two possible `block.prevrandao` values (skip or not skip the current RANDAO update) and computes both candidate `r = keccak256(keccak256(block.timestamp || prevrandao_candidate || victim || seed), x_provider)`.
5. The validator selects the `prevrandao` value that produces the favorable outcome and proposes the block accordingly.
6. The victim's `requestV2()` is included in that block; when the provider later calls `revealWithCallback`, the pre-selected outcome is delivered.

**Relevant code:** [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-310)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }

    function requestV2(
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(
            getDefaultProvider(),
            random(),
            gasLimit
        );
    }

    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(provider, random(), gasLimit);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1079-1089)
```text
    function random() internal returns (bytes32) {
        _state.seed = keccak256(
            abi.encodePacked(
                block.timestamp,
                block.prevrandao,
                msg.sender,
                _state.seed
            )
        );
        return _state.seed;
    }
```
