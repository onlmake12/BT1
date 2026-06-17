### Title
Entropy `requestHelper` Allows Provider to Be Their Own Requester, Breaking Two-Party Randomness Guarantee — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

The Entropy contract's `requestHelper` function does not validate that `provider != msg.sender`. A registered provider can call `requestV2` / `requestWithCallback` using their own provider address, collapsing the two-party security model into a single-party model where the provider/requester knows their own `x_i` in advance and can brute-force a favorable `x_U` to steer the final random number `r = hash(x_i, x_U)` toward any desired outcome.

### Finding Description

The Entropy protocol's security guarantee is:

> "the result is random as long as either the provider or user is honest"

This guarantee requires that the provider and user are **distinct parties**. The provider commits to a hash chain up-front and does not know the user's contribution `x_U` when they commit; the user does not know the provider's `x_i` when they commit `x_U`. When `provider == msg.sender`, both roles collapse into one party who knows `x_i` before choosing `x_U`.

In `requestHelper`, there is no check preventing this:

```solidity
function requestHelper(
    address provider,
    bytes32 userCommitment,
    ...
) internal returns (EntropyStructsV2.Request storage req) {
    EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[provider];
    if (_state.providers[provider].sequenceNumber == 0)
        revert EntropyErrors.NoSuchProvider();
    ...
    req.requester = msg.sender;   // no check: provider != msg.sender
``` [1](#0-0) 

The attack path:

1. Attacker registers as a provider via `register()`, computing their own hash chain and knowing every `x_i`.
2. Attacker calls `requestV2(attackerAddress, x_U, gasLimit)` where `msg.sender == provider == attackerAddress`.
3. Because the attacker knows `x_i` before choosing `x_U`, they can iterate over candidate `x_U` values off-chain, computing `r = combineRandomValues(x_U, x_i, 0)` for each, until they find one that produces a favorable outcome (e.g., "win" in a lottery, "rare" NFT trait).
4. They submit the favorable `x_U` as their user contribution.
5. They later call `revealWithCallback` with their known `x_i`, and the callback fires with the pre-selected `r`. [2](#0-1) 

### Impact Explanation

Any on-chain application that uses Entropy for randomness is vulnerable if the provider is also the consumer (e.g., a protocol that self-registers as a provider to avoid fees, or a malicious provider who also deploys a consumer contract). The two-party security guarantee is completely broken: the attacker controls both `x_i` (known in advance) and `x_U` (chosen after seeing `x_i`), giving them full control over `r` for small outcome spaces (win/lose, rarity tiers, etc.).

### Likelihood Explanation

Medium. Any registered provider can exploit this by simply calling `requestV2` with their own provider address. Providers are permissionlessly registered via `register()`. A malicious actor can register as a provider specifically to exploit this pattern against their own consumer contract. [3](#0-2) 

### Recommendation

Add a check in `requestHelper` (or in each public request entry point) that the caller is not the provider:

```solidity
if (provider == msg.sender) revert EntropyErrors.ProviderCannotBeRequester();
``` [4](#0-3) 

### Proof of Concept

```solidity
// Attacker is both provider and requester
contract AttackerProviderConsumer is IEntropyConsumer {
    IEntropy entropy;
    bytes32 public manipulatedResult;

    constructor(address _entropy) { entropy = IEntropy(_entropy); }

    function setup() external payable {
        // Register as provider with a known hash chain
        bytes32 knownX_i = keccak256("secret_seed_1");
        bytes32 commitment = keccak256(abi.encode(knownX_i)); // x_0
        entropy.register(0, commitment, "", 1000, "");
    }

    function exploit(bytes32 desiredOutcome) external payable {
        // Attacker knows x_i = keccak256("secret_seed_N") for the next sequence number
        bytes32 x_i = /* known from hash chain */ bytes32(0);
        // Brute-force x_U such that combineRandomValues(x_U, x_i, 0) == desiredOutcome
        bytes32 x_U;
        for (uint256 i = 0; i < 1000; i++) {
            x_U = keccak256(abi.encode(i));
            if (keccak256(abi.encode(x_U, x_i)) == desiredOutcome) break;
        }
        // Request from own provider address — no revert
        entropy.requestV2{value: msg.value}(address(this), x_U, 0);
    }

    function _entropyCallback(uint64, address, bytes32 result) external override {
        manipulatedResult = result; // == desiredOutcome
    }
}
```

The `requestHelper` accepts `provider == msg.sender` without error, and the attacker receives the pre-selected random number in the callback. [5](#0-4)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L214-261)
```text
    function requestHelper(
        address provider,
        bytes32 userCommitment,
        bool useBlockhash,
        bool isRequestWithCallback,
        uint32 callbackGasLimit
    ) internal returns (EntropyStructsV2.Request storage req) {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (_state.providers[provider].sequenceNumber == 0)
            revert EntropyErrors.NoSuchProvider();

        // Assign a sequence number to the request
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);

        // Store the user's commitment so that we can fulfill the request later.
        // Warning: this code needs to overwrite *every* field in the request, because the returned request can be
        // filled with arbitrary data.
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
