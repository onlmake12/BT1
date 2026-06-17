### Title
Entropy `numHashes` Inflation via Unrevealed Non-Callback Requests Blocks New Requests When `maxNumHashes` Is Set — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

An unprivileged user can make `maxNumHashes` non-callback requests without ever revealing them, inflating the `numHashes` counter for subsequent requests beyond the provider's configured limit. Once `numHashes > maxNumHashes`, every new request reverts with `LastRevealedTooOld`, blocking all users from obtaining randomness from that provider until the provider actively calls `advanceProviderCommitment`.

---

### Finding Description

In `requestHelper`, the `numHashes` field stored in each new request is computed as:

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
```

`assignedSequenceNumber` increments with every new request, but `currentCommitmentSequenceNumber` only advances when a request is actually revealed on-chain. The guard that prevents this from growing unboundedly is:

```solidity
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
```

For **callback** requests (`requestV2`), anyone — including the Fortuna keeper — can call `revealWithCallback`, so the keeper naturally advances `currentCommitmentSequenceNumber` and keeps `numHashes` small. However, for **non-callback** requests (`request`), the `reveal` function enforces:

```solidity
if (req.requester != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
```

Only the original requester can reveal a non-callback request. An attacker who submits `maxNumHashes` non-callback requests and never calls `reveal` permanently holds `currentCommitmentSequenceNumber` at its current value. Every subsequent request by any user will compute `numHashes > maxNumHashes` and revert.

The provider's only recourse is `advanceProviderCommitment`, which itself runs the same hash loop:

```solidity
uint32 numHashes = SafeCast.toUint32(
    advancedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
bytes32 providerCommitment = constructProviderCommitment(
    numHashes,
    providerContribution
);
```

If the attacker has accumulated a large gap, the provider must advance in small increments across multiple transactions, requiring active monitoring and sustained gas expenditure. Meanwhile the attacker can immediately re-flood with new non-callback requests to re-trigger the block.

---

### Impact Explanation

All new randomness requests to the targeted provider revert with `LastRevealedTooOld` for as long as the attacker sustains the attack. Contracts that depend on Pyth Entropy for lotteries, NFT mints, gaming outcomes, or any other on-chain randomness are unable to obtain new random numbers. The Fortuna keeper cannot unblock the situation on its own because it cannot reveal non-callback requests on behalf of the attacker.

---

### Likelihood Explanation

Any unprivileged address can call `request()` and pay the provider fee. If `maxNumHashes` is set to a typical value (e.g., 100–1 000) and the per-request fee is small (e.g., a few cents on a low-fee chain), the cost to trigger the block is on the order of a few dollars. The attacker recovers nothing (fees go to the provider), but the attack can be repeated indefinitely at the same cost per cycle, making it a cheap, repeatable griefing vector.

---

### Recommendation

1. **Track per-user pending non-callback requests** and subtract them from the user's "available" balance before accepting a new request, or cap the number of outstanding non-callback requests per address.
2. **Allow the provider (or an authorized operator) to cancel stale non-callback requests** and advance `currentCommitmentSequenceNumber` without requiring the original requester's cooperation.
3. **Require a refundable deposit** for non-callback requests that is forfeited if the request is not revealed within a time window, creating an economic disincentive for abandonment.
4. **Alternatively, remove the asymmetry**: allow `revealWithCallback`-style permissionless fulfillment for non-callback requests as well, so the keeper can always clear the queue.

---

### Proof of Concept

1. Provider registers with `maxNumHashes = 100`.
2. Attacker calls `request(provider, commitment, false)` 100 times, paying the fee each time. Each call is a non-callback request; `assignedSequenceNumber` advances from `S` to `S+100` while `currentCommitmentSequenceNumber` stays at `S`.
3. A legitimate user calls `requestV2(provider, ...)`. Inside `requestHelper`, `numHashes = (S+101) - S = 101 > 100`, so the call reverts with `LastRevealedTooOld`.
4. The Fortuna keeper cannot reveal the attacker's 100 requests (they are non-callback; `reveal` requires `msg.sender == req.requester`).
5. The provider must call `advanceProviderCommitment(provider, S+100, proof_S+100)` to advance past the attacker's requests. This costs gas and requires the provider to be monitoring.
6. Immediately after step 5, the attacker repeats from step 2, re-blocking the provider at a cost of 100 × fee per cycle.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-255)
```text
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L496-515)
```text
    function reveal(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override returns (bytes32 randomNumber) {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1048-1068)
```text
    function allocRequest(
        address provider,
        uint64 sequenceNumber
    ) internal returns (EntropyStructsV2.Request storage req) {
        (, uint8 shortKey) = requestKey(provider, sequenceNumber);

        req = _state.requests[shortKey];
        if (isActive(req)) {
            // There's already a prior active request in the storage slot we want to use.
            // Overflow the prior request to the requestsOverflow mapping.
            // It is important that this code overflows the *prior* request to the mapping, and not the new request.
            // There is a chance that some requests never get revealed and remain active forever. We do not want such
            // requests to fill up all of the space in the array and cause all new requests to incur the higher gas cost
            // of the mapping.
            //
            // This operation is expensive, but should be rare. If overflow happens frequently, increase
            // the size of the requests array to support more concurrent active requests.
            (bytes32 reqKey, ) = requestKey(req.provider, req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L45-51)
```text
contract EntropyState {
    // The size of the requests hash table. Must be a power of 2.
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;

    EntropyInternalStructs.State _state;
}
```
