### Title
Entropy Provider Can Selectively Censor Randomness Reveals, Permanently Freezing User Requests and Fees — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The Pyth Entropy protocol allows any registered provider to selectively refuse to reveal their committed random number for specific sequence numbers. Because only the provider possesses the hash-chain value `x_i`, and the contract has no timeout or refund mechanism, a malicious provider can permanently freeze user randomness requests and retain the fees paid — an exact structural analog to the Linea prover censoring L2→L1 messages.

---

### Finding Description

When a user calls `requestWithCallback`, the contract stores the request with `callbackStatus = CALLBACK_NOT_STARTED` and collects the fee upfront. [1](#0-0) 

The stored `req.commitment` is `keccak256(userCommitment, providerInfo.currentCommitment)` and `req.numHashes` is `assignedSequenceNumber - providerInfo.currentCommitmentSequenceNumber`. Both are fixed at request time. [2](#0-1) 

To fulfill the request, `revealWithCallback` must be called with a `providerContribution` satisfying:

```
hash^numHashes(providerContribution) == providerInfo.currentCommitment
``` [3](#0-2) 

Only the provider knows the hash-chain value `x_i`. The protocol design documentation explicitly acknowledges the resulting attack surface:

> *"Providers are trusted to reveal their random number (x_i) regardless of what the final result (r) is. Providers can compute (r) off-chain before they reveal (x_i), which permits a censorship attack."* [4](#0-3) 

The contract contains **no timeout**, **no refund path**, and **no alternative fulfillment mechanism**. The fee accrues permanently to `providerInfo.accruedFeesInWei` even if the provider never reveals. [5](#0-4) 

The `revealWithCallback` function is the sole path to clear a `CALLBACK_NOT_STARTED` or `CALLBACK_FAILED` request: [6](#0-5) 

There is no on-chain mechanism for a user to reclaim their fee or force fulfillment after a timeout.

---

### Impact Explanation

- **User randomness requests are permanently frozen**: The `callbackStatus` remains `CALLBACK_NOT_STARTED` indefinitely; the application callback is never delivered.
- **Fees are permanently lost**: The fee paid by the user is credited to `providerInfo.accruedFeesInWei` at request time and is never refunded.
- **Selective outcome manipulation**: The provider can compute `r = hash(x_i, x_U)` off-chain before deciding whether to reveal. This allows the provider to censor only requests with unfavorable outcomes (e.g., a user winning a lottery), while fulfilling all others — making the attack undetectable from aggregate statistics.
- **Application-level DoS**: Any smart contract depending on the `_entropyCallback` to proceed (e.g., a game, a lottery, a VRF-gated action) is permanently blocked.

---

### Likelihood Explanation

- **Permissionless provider registration**: Any address can call `register()` to become a provider, attract users, collect fees, and then selectively censor.
- **Trivially easy execution**: The attack requires only omitting a transaction — no exploit code, no key compromise.
- **Economically motivated**: A provider can front-run user requests off-chain, compute the outcome, and censor only losing-for-provider results.
- **Default provider risk**: The default provider (Fortuna, operated by Douro Labs) is a single centralized entity. Negligence or compromise of this entity affects all users relying on the default. [7](#0-6) 

---

### Recommendation

1. **Add a request expiry / refund mechanism**: After a configurable timeout (e.g., 256 blocks), allow users to call a `refund()` function that returns their fee if the provider has not revealed.
2. **Provider collateral / slashing**: Require providers to post a bond that is slashable if they fail to reveal within the timeout window.
3. **Multi-provider fallback**: Allow users to designate a backup provider that can fulfill the request if the primary provider fails to reveal within the timeout.
4. **On-chain SLA enforcement**: Emit a `RevealDeadlineMissed` event after the timeout, enabling off-chain monitoring and reputation systems.

---

### Proof of Concept

1. Provider registers with `register(feeInWei, commitment, metadata, chainLength, uri)`.
2. User calls `requestWithCallback{value: fee}(provider, userRandomNumber)` — fee is deducted, `req.callbackStatus = CALLBACK_NOT_STARTED`, `req.sequenceNumber = N`.
3. Provider computes `r = combineRandomValues(x_N, userRandomNumber, 0)` off-chain.
4. If `r` is unfavorable (e.g., user wins), provider simply **does not call** `revealWithCallback`.
5. `req.callbackStatus` remains `CALLBACK_NOT_STARTED` forever.
6. User's application never receives `_entropyCallback`. Fee is permanently locked in `providerInfo.accruedFeesInWei`.
7. No on-chain path exists for the user to recover their fee or force fulfillment. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L244-267)
```text
        req = allocRequest(provider, assignedSequenceNumber);
        req.provider = provider;
        req.sequenceNumber = assignedSequenceNumber;
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
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
        req.requester = msg.sender;

        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;

        req.callbackStatus = isRequestWithCallback
            ? EntropyStatusConstants.CALLBACK_NOT_STARTED
            : EntropyStatusConstants.CALLBACK_NOT_NECESSARY;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L400-408)
```text
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-566)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        bytes32 randomNumber;
        (randomNumber, ) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
```

**File:** apps/developer-hub/content/docs/entropy/protocol-design.mdx (L52-52)
```text
- Providers are trusted to reveal their random number $$(x_i)$$ regardless of what the final result $$(r)$$ is. Providers can compute $$(r)$$ off-chain before they reveal $$(x_i)$$, which permits a censorship attack.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L6-42)
```text
    struct ProviderInfo {
        uint128 feeInWei;
        uint128 accruedFeesInWei;
        // The commitment that the provider posted to the blockchain, and the sequence number
        // where they committed to this. This value is not advanced after the provider commits,
        // and instead is stored to help providers track where they are in the hash chain.
        bytes32 originalCommitment;
        uint64 originalCommitmentSequenceNumber;
        // Metadata for the current commitment. Providers may optionally use this field to help
        // manage rotations (i.e., to pick the sequence number from the correct hash chain).
        bytes commitmentMetadata;
        // Optional URI where clients can retrieve revelations for the provider.
        // Client SDKs can use this field to automatically determine how to retrieve random values for each provider.
        // TODO: specify the API that must be implemented at this URI
        bytes uri;
        // The first sequence number that is *not* included in the current commitment (i.e., an exclusive end index).
        // The contract maintains the invariant that sequenceNumber <= endSequenceNumber.
        // If sequenceNumber == endSequenceNumber, the provider must rotate their commitment to add additional random values.
        uint64 endSequenceNumber;
        // The sequence number that will be assigned to the next inbound user request.
        uint64 sequenceNumber;
        // The current commitment represents an index/value in the provider's hash chain.
        // These values are used to verify requests for future sequence numbers. Note that
        // currentCommitmentSequenceNumber < sequenceNumber.
        //
        // The currentCommitment advances forward through the provider's hash chain as values
        // are revealed on-chain.
        bytes32 currentCommitment;
        uint64 currentCommitmentSequenceNumber;
        // An address that is authorized to set / withdraw fees on behalf of this provider.
        address feeManager;
        // Maximum number of hashes to record in a request. This should be set according to the maximum gas limit
        // the provider supports for callbacks.
        uint32 maxNumHashes;
        // Default gas limit to use for callbacks.
        uint32 defaultGasLimit;
    }
```

**File:** apps/fortuna/src/keeper/commitment.rs (L57-70)
```rust
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

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L1-50)
```text
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;

library EntropyErrors {
    // An invariant of the contract failed to hold. This error indicates a software logic bug.
    // Signature: 0xd82dd966
    error AssertionFailure();
    // The provider being registered has already registered
    // Signature: 0xda041bdf
    error ProviderAlreadyRegistered();
    // The requested provider does not exist.
    // Signature: 0xdf51c431
    error NoSuchProvider();
    // The specified request does not exist.
    // Signature: 0xc4237352
    error NoSuchRequest();
    // The randomness provider is out of commited random numbers. The provider needs to
    // rotate their on-chain commitment to resolve this error.
    // Signature: 0x3e515085
    error OutOfRandomness();
    // The transaction fee was not sufficient
    // Signature: 0x025dbdd4
    error InsufficientFee();
    // Either the user's or the provider's revealed random values did not match their commitment.
    // Signature: 0xb8be1a8d
    error IncorrectRevelation();
    // Governance message is invalid (e.g., deserialization error).
    // Signature: 0xb463ce7a
    error InvalidUpgradeMagic();
    // The msg.sender is not allowed to invoke this call.
    // Signature: 0x82b42900
    error Unauthorized();
    // The blockhash is 0.
    // Signature: 0x92555c0e
    error BlockhashUnavailable();
    // if a request was made using `requestWithCallback`, request should be fulfilled using `revealWithCallback`
    // else if a request was made using `request`, request should be fulfilled using `reveal`
    // Signature: 0x50f0dc92
    error InvalidRevealCall();
    // The last random number revealed from the provider is too old. Therefore, too many hashes
    // are required for any new reveal. Please update the currentCommitment before making more requests.
    error LastRevealedTooOld();
    // A more recent commitment is already revealed on-chain
    error UpdateTooOld();
    // Not enough gas was provided to the function to execute the callback with the desired amount of gas.
    error InsufficientGas();
    // A gas limit value was provided that was greater than the maximum possible limit of 655,350,000
    error MaxGasLimitExceeded();
}
```
