### Title
Unpermissioned `advanceProviderCommitment()` Allows Any Caller to Burn Provider Randomness Slots and Disrupt Pending Requests - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.advanceProviderCommitment()` is declared `public` with no `msg.sender == provider` check. Any caller who supplies a valid hash-chain value (which is publicly observable on-chain from prior `reveal()` / `revealWithCallback()` transactions) can advance a provider's `currentCommitmentSequenceNumber` and `currentCommitment` to an arbitrary future position, burning randomness slots and disrupting in-flight requests.

---

### Finding Description

`advanceProviderCommitment()` is intended to let a provider "fast-forward" their commitment pointer to reduce the number of hashes required for future reveals. The function's only protection is a cryptographic check: the supplied `providerContribution` must hash (via `constructProviderCommitment`) to the current `providerInfo.currentCommitment`.

```solidity
function advanceProviderCommitment(
    address provider,
    uint64 advancedSequenceNumber,
    bytes32 providerContribution
) public override {
``` [1](#0-0) 

There is no `require(msg.sender == provider)` or equivalent guard. The cryptographic check is the sole barrier:

```solidity
if (providerCommitment != providerInfo.currentCommitment)
    revert EntropyErrors.IncorrectRevelation();
``` [2](#0-1) 

However, every `reveal()` and `revealWithCallback()` call emits the provider's `providerContribution` as a function argument and stores it in `providerInfo.currentCommitment`. These values are permanently visible in calldata on any public EVM chain. An attacker can replay any previously revealed hash-chain value to satisfy the cryptographic check.

The critical side-effect: if the attacker advances `currentCommitmentSequenceNumber` to a value ≥ `sequenceNumber`, the contract bumps `sequenceNumber` forward, permanently burning those slots:

```solidity
if (
    providerInfo.currentCommitmentSequenceNumber >=
    providerInfo.sequenceNumber
) {
    providerInfo.sequenceNumber =
        providerInfo.currentCommitmentSequenceNumber + 1;
}
``` [3](#0-2) 

---

### Impact Explanation

1. **Randomness slot exhaustion (DoS):** An attacker can repeatedly advance the commitment pointer to positions ≥ `sequenceNumber`, burning large blocks of the provider's hash chain. Once `sequenceNumber` reaches `endSequenceNumber`, the provider is out of randomness and all new requests revert with `OutOfRandomness`. This is a griefing attack that forces the provider to re-register with a new chain.

2. **`LastRevealedTooOld` for pending requests:** When `maxNumHashes` is set, `requestHelper` checks `req.numHashes > providerInfo.maxNumHashes`. Advancing `currentCommitmentSequenceNumber` far ahead of pending requests increases `numHashes` for those requests, causing them to revert with `LastRevealedTooOld` and permanently blocking fulfillment of in-flight randomness requests. [4](#0-3) 

---

### Likelihood Explanation

- The attack requires no privileged access, no leaked keys, and no governance majority.
- The required `providerContribution` values are publicly available in calldata of any prior `reveal()` or `revealWithCallback()` transaction on the same chain.
- The Fortuna keeper service (`apps/fortuna`) continuously calls `revealWithCallback`, making fresh hash-chain values available on every block.
- The attacker only needs to submit one transaction per advance step. The cost is a single gas payment. [5](#0-4) 

---

### Recommendation

Add a `msg.sender == provider` check at the top of `advanceProviderCommitment()`:

```solidity
function advanceProviderCommitment(
    address provider,
    uint64 advancedSequenceNumber,
    bytes32 providerContribution
) public override {
+   if (msg.sender != provider) revert EntropyErrors.Unauthorized();
    ...
}
```

Alternatively, restrict to `msg.sender == provider || msg.sender == providerInfo.feeManager`, consistent with the pattern used in `setProviderFeeAsFeeManager()` and `withdrawAsFeeManager()`. [6](#0-5) 

---

### Proof of Concept

1. Provider `P` registers with a hash chain of length 10,000 and calls `setMaxNumHashes(50)`.
2. User `U` calls `requestV2(P, ...)` → assigned `sequenceNumber = 5`.
3. Attacker `A` watches the mempool/chain for any prior `revealWithCallback` transaction from `P`, extracting `providerContribution_100` (the hash-chain value at position 100).
4. `A` calls:
   ```solidity
   entropy.advanceProviderCommitment(
       P,
       100,          // advancedSequenceNumber > sequenceNumber (6)
       providerContribution_100
   );
   ```
5. The contract sets `currentCommitmentSequenceNumber = 100` and bumps `sequenceNumber = 101`, burning slots 6–100.
6. When the Fortuna keeper tries to fulfill request #5, `req.numHashes = 100 - 0 = 100 > maxNumHashes(50)` → reverts `LastRevealedTooOld`. User `U`'s randomness request is permanently stuck.
7. `A` repeats, advancing to 200, 300, … until `sequenceNumber` reaches `endSequenceNumber`, at which point all new requests revert `OutOfRandomness`. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L251-256)
```text
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L443-484)
```text
    function advanceProviderCommitment(
        address provider,
        uint64 advancedSequenceNumber,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (
            advancedSequenceNumber <=
            providerInfo.currentCommitmentSequenceNumber
        ) revert EntropyErrors.UpdateTooOld();
        if (advancedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.AssertionFailure();

        uint32 numHashes = SafeCast.toUint32(
            advancedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        bytes32 providerCommitment = constructProviderCommitment(
            numHashes,
            providerContribution
        );

        if (providerCommitment != providerInfo.currentCommitment)
            revert EntropyErrors.IncorrectRevelation();

        providerInfo.currentCommitmentSequenceNumber = advancedSequenceNumber;
        providerInfo.currentCommitment = providerContribution;
        if (
            providerInfo.currentCommitmentSequenceNumber >=
            providerInfo.sequenceNumber
        ) {
            // This means the provider called the function with a sequence number that was not yet requested.
            // Providers should never do this and we consider such an implementation flawed.
            // Assuming this is landed on-chain it's better to bump the sequence number and never use that range
            // for future requests. Otherwise, someone can use the leaked revelation to derive favorable random numbers.
            providerInfo.sequenceNumber =
                providerInfo.currentCommitmentSequenceNumber +
                1;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L829-843)
```text
    function setProviderFeeAsFeeManager(
        address provider,
        uint128 newFeeInWei
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```
