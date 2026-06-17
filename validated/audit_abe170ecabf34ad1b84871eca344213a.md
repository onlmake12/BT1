### Title
Provider Front-Running via Plaintext `userContribution` Exposure in `requestWithCallback` / `requestV2` - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`requestWithCallback` and `requestV2` accept the user's raw random contribution (`userContribution`) as a plaintext calldata argument and immediately emit it in two on-chain events. Because the provider knows their own hash chain, they can compute the final random number `r = hash(userContribution, x_i, 0)` the moment they observe the pending transaction in the mempool — before the request is even mined. This allows a malicious provider to front-run the user's request by inserting their own requests to shift the sequence number assignment, selecting whichever `x_i` produces a result favorable to them.

---

### Finding Description

`requestWithCallback` delegates to `requestV2`, which accepts `userContribution` as the raw preimage:

```solidity
function requestV2(
    address provider,
    bytes32 userContribution,   // raw secret value, not its hash
    uint32 gasLimit
) public payable override returns (uint64) {
    EntropyStructsV2.Request storage req = requestHelper(
        provider,
        constructUserCommitment(userContribution),  // hashed only for storage
        false, true, gasLimit
    );
    emit RequestedWithCallback(
        provider, req.requester, req.sequenceNumber,
        userContribution,   // raw value broadcast on-chain
        EntropyStructConverter.toV1Request(req)
    );
    emit EntropyEventsV2.Requested(
        provider, req.requester, req.sequenceNumber,
        userContribution,   // raw value broadcast on-chain
        uint32(req.gasLimit10k) * TEN_THOUSAND, bytes("")
    );
    return req.sequenceNumber;
}
``` [1](#0-0) 

The raw `userContribution` is visible in:
1. **Transaction calldata** — observable in the public mempool before the block is mined.
2. **`RequestedWithCallback` event** — permanently on-chain.
3. **`EntropyEventsV2.Requested` event** — permanently on-chain. [2](#0-1) 

By contrast, the legacy `request()` function requires the caller to pass `userCommitment` (the hash), keeping the preimage secret until the user's own `reveal()` call:

```solidity
function request(
    address provider,
    bytes32 userCommitment,   // hash only — preimage stays secret
    bool useBlockHash
) public payable override returns (uint64 assignedSequenceNumber) {
``` [3](#0-2) 

The final random number is computed as:

```solidity
randomNumber = combineRandomValues(userContribution, providerContribution, blockHash);
``` [4](#0-3) 

Because `useBlockhash` is hardcoded to `false` in `requestV2`, `blockHash = 0`, so `r = keccak256(userContribution, x_i, 0)`. A provider who knows their hash chain value `x_i` for the next sequence number can compute `r` the instant they see `userContribution` in the mempool.

---

### Impact Explanation

A malicious provider can:

1. Observe a pending `requestWithCallback(provider, userContribution)` transaction in the mempool.
2. Compute `r = keccak256(userContribution ‖ x_i ‖ 0)` for the next sequence number `i`.
3. If `r` is unfavorable (e.g., user wins a lottery), insert one or more of their own `requestWithCallback` calls with higher gas to bump the sequence number, so the user's request lands at `i+k` instead of `i`.
4. Compute `r' = keccak256(userContribution ‖ x_{i+k} ‖ 0)` and repeat until a favorable result is found.
5. Reveal the chosen `x_j` to deliver the manipulated result.

Any application using Pyth Entropy for high-value randomness (lotteries, NFT rarity, on-chain games) is vulnerable to result manipulation by a malicious provider. Because provider registration is permissionless, an attacker can register as a provider, offer competitive fees to attract users, and then manipulate results at will. [5](#0-4) 

---

### Likelihood Explanation

Provider registration is fully permissionless via `register()`. Any address can become a provider by posting a hash chain commitment. A malicious actor can register, undercut legitimate providers on fees, and execute this attack against every `requestWithCallback` user. The attack requires only mempool monitoring and the ability to submit transactions with higher gas — both trivially achievable. The `userContribution` is also permanently emitted in events, so even post-block analysis enables retroactive verification of manipulation. [6](#0-5) 

---

### Recommendation

Change `requestWithCallback` / `requestV2` to accept `userCommitment = keccak256(userContribution)` instead of the raw `userContribution`, consistent with the `request()` function. The raw preimage should only be revealed at `revealWithCallback` time. Events should emit the commitment (hash), not the preimage. This prevents the provider from computing `r` before the request is mined.

---

### Proof of Concept

```
1. Attacker registers as a provider with a known hash chain [x_0, x_1, ..., x_N].
2. Victim calls: requestWithCallback(attacker_provider, userContribution)
   → Transaction is pending in mempool with userContribution visible in calldata.
3. Attacker reads userContribution from mempool.
4. Attacker computes r = keccak256(userContribution ‖ x_nextSeq ‖ 0).
5. If r is unfavorable (e.g., victim wins), attacker submits their own
   requestWithCallback(attacker_provider, anyValue) with higher gas,
   bumping the sequence number.
6. Attacker recomputes r' = keccak256(userContribution ‖ x_{nextSeq+1} ‖ 0).
7. Attacker repeats until a favorable r is found.
8. Attacker calls revealWithCallback(attacker_provider, victimSeqNum,
   userContribution, x_j) delivering the manipulated result.
9. Victim's callback receives the attacker-chosen random number.
```

The `userContribution` emitted in `EntropyEventsV2.Requested` confirms the attack is also executable post-block by any observer with the hash chain. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L51-54)
```text
// This protocol has the same security properties as the 2-party randomness protocol above: as long as either
// the provider or user is honest, the number r is random. Note that this analysis assumes that
// providers cannot frontrun user transactions -- a dishonest provider who frontruns user transaction can
// manipulate the result.
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L322-336)
```text
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) public payable override returns (uint64 assignedSequenceNumber) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            userCommitment,
            useBlockHash,
            false,
            0
        );
        assignedSequenceNumber = req.sequenceNumber;
        emit Requested(EntropyStructConverter.toV1Request(req));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-390)
```text
    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );

        emit RequestedWithCallback(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            EntropyStructConverter.toV1Request(req)
        );
        emit EntropyEventsV2.Requested(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            uint32(req.gasLimit10k) * TEN_THOUSAND,
            bytes("")
        );
        return req.sequenceNumber;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L426-430)
```text
        randomNumber = combineRandomValues(
            userContribution,
            providerContribution,
            blockHash
        );
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyEventsV2.sol (L30-37)
```text
    event Requested(
        address indexed provider,
        address indexed caller,
        uint64 indexed sequenceNumber,
        bytes32 userContribution,
        uint32 gasLimit,
        bytes extraArgs
    );
```
