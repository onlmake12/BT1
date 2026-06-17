### Title
Malicious Entropy Requester Contract Can Permanently Block Request Fulfillment via Reverting Callback - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
In `Entropy.sol`, the `revealWithCallback` function contains two code paths that call the requester's `_entropyCallback` directly without catching reverts. A malicious requester contract that always reverts in `_entropyCallback` can permanently prevent its own request from ever being fulfilled or cleared, causing the provider's keeper (Fortuna) to waste gas indefinitely and leaving the request permanently stuck in contract storage.

### Finding Description
`revealWithCallback` branches into two paths based on `req.gasLimit10k` and `req.callbackStatus`:

**Path 1 — New flow** (`gasLimit10k != 0 && callbackStatus == CALLBACK_NOT_STARTED`): Uses `excessivelySafeCall` to catch reverts and transitions the request to `CALLBACK_FAILED` on failure. This path is safe.

**Path 2 — Old/recovery flow** (else branch, taken when `gasLimit10k == 0` OR `callbackStatus == CALLBACK_FAILED`): Clears the request first (CEI), then calls `_entropyCallback` directly with no gas cap and no revert catching:

```solidity
clearRequest(provider, sequenceNumber);
// ...
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

If `_entropyCallback` reverts, the entire transaction reverts — including the `clearRequest`. The request is restored to its prior state (`CALLBACK_NOT_STARTED` for old providers, `CALLBACK_FAILED` for the recovery path). Because `revealHelper` also reverts, the provider's `currentCommitment` is not advanced. The keeper can retry with the same `providerContribution`, the commitment check passes again, and the callback reverts again — indefinitely.

The `CALLBACK_IN_PROGRESS` guard used in Path 1 is absent in Path 2: [2](#0-1) 

The `revealHelper` commitment validation logic that runs before the callback: [3](#0-2) 

The entry guard that allows `CALLBACK_FAILED` requests into Path 2: [4](#0-3) 

### Impact Explanation
- **Permanent DoS of a specific request**: The request can never be cleared from storage. The requester loses their paid fee and never receives a random number.
- **Keeper gas drain**: The Fortuna keeper (`apps/fortuna`) will repeatedly attempt `revealWithCallback` for the stuck request, burning gas on every attempt with no possibility of success.
- **Storage bloat**: The stuck request occupies a storage slot indefinitely.
- **Application-level cascade**: If the requester is a shared contract (e.g., a lottery or game used by many end users), all downstream users are permanently blocked from receiving their random numbers.

### Likelihood Explanation
- **Old provider path** (`gasLimit10k == 0`): Any provider that has not called `setDefaultGasLimit` is vulnerable. A malicious user only needs to make a request with such a provider using a reverting contract.
- **Recovery path** (`CALLBACK_FAILED`): Any request that has already failed its first callback attempt enters this state. A requester whose callback always reverts will permanently block the recovery.
- Both conditions are reachable by an unprivileged user with no special access.

### Recommendation
1. **For the `gasLimit10k == 0` path**: Wrap the direct `_entropyCallback` call in a `try/catch` or use `excessivelySafeCall` (as is done in Path 1). On revert, emit a failure event and clear the request anyway, or transition to a `CALLBACK_FAILED`-equivalent state.
2. **For the `CALLBACK_FAILED` recovery path**: The current design intentionally propagates reverts to expose the revert reason. Consider adding an admin/governance escape hatch to forcibly clear permanently stuck requests, or document a timeout after which the request can be cleared by anyone.
3. Providers should be encouraged to set `defaultGasLimit` to migrate away from the old flow.

### Proof of Concept

```solidity
// Malicious requester contract
contract MaliciousRequester is IEntropyConsumer {
    address entropy;
    constructor(address _entropy) { entropy = _entropy; }

    function getEntropy() internal view override returns (address) {
        return entropy;
    }

    function entropyCallback(
        uint64, address, bytes32
    ) internal override {
        revert("blocked"); // always revert
    }

    function makeRequest(address provider, bytes32 userRandom) external payable {
        uint128 fee = IEntropy(entropy).getFee(provider);
        IEntropy(entropy).requestWithCallback{value: fee}(provider, userRandom);
    }
}
```

**Attack steps:**
1. Deploy `MaliciousRequester` pointing at the Entropy contract.
2. Call `makeRequest` with a provider whose `defaultGasLimit == 0` (old provider).
3. The provider's keeper calls `revealWithCallback` — the transaction reverts because `_entropyCallback` reverts.
4. The request is restored to `CALLBACK_NOT_STARTED`. The keeper retries and always fails.
5. The request is permanently stuck; the keeper burns gas on every retry.

The `gasLimit10k == 0` condition is set at request time: [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-438)
```text
    function revealHelper(
        EntropyStructsV2.Request storage req,
        bytes32 userContribution,
        bytes32 providerContribution
    ) internal returns (bytes32 randomNumber, bytes32 blockHash) {
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();

        blockHash = bytes32(uint256(0));
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();

            blockHash = _blockHash;
        }

        randomNumber = combineRandomValues(
            userContribution,
            providerContribution,
            blockHash
        );

        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            req.provider
        ];
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L553-559)
```text
        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```
