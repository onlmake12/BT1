### Title
Unprivileged User Can Exhaust `maxNumHashes` Window to Block New Entropy Requests - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

An unprivileged user can make `maxNumHashes` requests to the Entropy contract and deliberately never call `reveal`, causing `currentCommitmentSequenceNumber` to stagnate. Once the gap between the next assignable sequence number and `currentCommitmentSequenceNumber` exceeds `maxNumHashes`, every subsequent request from any user reverts with `LastRevealedTooOld`, temporarily halting the provider's service until the provider manually calls `advanceProviderCommitment`.

---

### Finding Description

The Entropy contract tracks a provider's hash-chain position via `currentCommitmentSequenceNumber`. Each new request records:

```solidity
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

`currentCommitmentSequenceNumber` only advances inside `revealHelper`, and only when a reveal actually occurs:

```solidity
if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
    providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
    providerInfo.currentCommitment = providerContribution;
}
``` [2](#0-1) 

The `reveal` function enforces that only the original requester can call it:

```solidity
if (req.requester != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
``` [3](#0-2) 

This means the provider **cannot force-reveal** a user's request. If a user makes a request via `request()` (non-callback path) and never calls `reveal`, the provider has no way to advance `currentCommitmentSequenceNumber` for that slot except by calling `advanceProviderCommitment` — which requires knowing the hash-chain value at that position and submitting an on-chain transaction.

The attack mirrors the Cardex nonce-blocking pattern exactly:

- Alice requests at sequence N (pays fee, never reveals)
- Bob requests at sequence N+1 (pays fee, never reveals)
- … repeated `maxNumHashes` times
- The next legitimate user's request computes `numHashes = (N + maxNumHashes + 1) - currentCommitmentSequenceNumber > maxNumHashes` → `LastRevealedTooOld` revert [4](#0-3) 

---

### Impact Explanation

All new randomness requests to the targeted provider are blocked until the provider calls `advanceProviderCommitment`. The Fortuna keeper already monitors for this condition and logs a warning:

```rust
tracing::warn!("Update commitments threshold reached -- possible outage or DDOS attack...");
``` [5](#0-4) 

However, the keeper only triggers at 95% of `maxNumHashes` outstanding requests, and the `advanceProviderCommitment` call requires an on-chain transaction from the provider. During the window between the attack completing and the keeper responding, all new Entropy requests to that provider revert. Downstream contracts relying on randomness (e.g., NFT mints, games, lotteries) are denied service.

**Impact: Medium** — temporary but complete DoS of new randomness requests for a provider; existing in-flight requests are unaffected.

---

### Likelihood Explanation

The attacker must pay `maxNumHashes × fee` in native tokens. For the default Fortuna provider, `maxNumHashes` is set to a value that limits gas cost per reveal. At typical EVM fees and provider fees, the cost is non-trivial but feasible for a motivated attacker targeting a high-value application. The attack is permissionless (any EOA or contract can call `request()`), requires no privileged access, and can be repeated after each provider mitigation.

**Likelihood: Medium** — requires capital but is fully permissionless and repeatable.

---

### Recommendation

1. **Allow the provider (or anyone) to cancel/skip unrevealed `request()` slots** after a timeout, advancing `currentCommitmentSequenceNumber` without requiring the original requester's secret.
2. **Alternatively, remove the `req.requester == msg.sender` restriction on `reveal`** so the provider can force-reveal any slot using their own hash-chain value, eliminating the stagnation vector entirely (the random number is already determined by the provider's hash chain; the user's contribution only adds entropy, not authorization).
3. **Rate-limit requests per address** or require a refundable bond that is slashed if the request is not revealed within a time window.

---

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

    // Call this maxNumHashes times with sufficient fee each time
    function spamRequest(bytes32 userCommitment) external payable {
        // request() path — only msg.sender can reveal
        entropy.request{value: msg.value}(provider, userCommitment, false);
        // Never call reveal() — commitment stagnates
    }
}
```

After `maxNumHashes` calls to `spamRequest`, any subsequent call by a legitimate user to `request()` or `requestV2()` reverts with `LastRevealedTooOld`: [6](#0-5) 

The provider must call `advanceProviderCommitment` with the correct hash-chain value to restore service: [7](#0-6) 

The Fortuna keeper's existing monitoring confirms Pyth is aware of this attack surface: [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L513-515)
```text
        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```

**File:** apps/fortuna/src/keeper/commitment.rs (L55-70)
```rust
    let threshold =
        ((provider_info.max_num_hashes as f64) * UPDATE_COMMITMENTS_THRESHOLD_FACTOR) as u64;
    let outstanding_requests =
        provider_info.sequence_number - provider_info.current_commitment_sequence_number;
    if outstanding_requests > threshold {
        // NOTE: This log message triggers a grafana alert. If you want to change the text, please change the alert also.
        tracing::warn!("Update commitments threshold reached -- possible outage or DDOS attack. Number of outstanding requests: {:?} Threshold: {:?}", outstanding_requests, threshold);
        let seq_number = provider_info.sequence_number - 1;
        let provider_revelation = chain_state
            .state
            .reveal(seq_number)
            .map_err(|e| anyhow!("Error revealing: {:?}", e))?;
        let contract_call =
            contract.advance_provider_commitment(provider_address, seq_number, provider_revelation);
        send_and_confirm(contract_call).await?;
    }
```
