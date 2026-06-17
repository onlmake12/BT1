### Title
Permanently Stuck Entropy Requests via Reverting Callback in Legacy `revealWithCallback` Path — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains a legacy execution path (triggered when `req.gasLimit10k == 0`) that calls the requester's `_entropyCallback` directly without any try/catch or safe-call wrapper. A malicious requester can deploy a contract whose callback always reverts, permanently preventing the provider from fulfilling the request. The request becomes permanently stuck with no on-chain recovery path, mirroring the JOJO pattern where a user-controlled address blocks a critical protocol function.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

- **New path** (`req.gasLimit10k != 0`, lines 574–660): uses `excessivelySafeCall`, catches reverts, transitions to `CALLBACK_FAILED` state, and provides a recovery mechanism.
- **Legacy path** (`req.gasLimit10k == 0`, lines 661–702): calls `IEntropyConsumer(callAddress)._entropyCallback(...)` directly with no try/catch and no recovery state. [1](#0-0) 

The legacy path is entered whenever `req.gasLimit10k == 0`. This value is set in `requestHelper` when `providerInfo.defaultGasLimit == 0`: [2](#0-1) 

`defaultGasLimit` is zero by default for every provider that has not explicitly called `setDefaultGasLimit`. This is the default state for any provider registered before the new callback-failure flow was introduced. [3](#0-2) 

In the legacy path, `clearRequest` is called **before** the callback: [4](#0-3) 

If the callback reverts, the entire transaction reverts (including `clearRequest`), so the request remains active. Because there is no `CALLBACK_FAILED` state and no `recoverCallback` equivalent for this path, the request is permanently stuck. The provider can never clear it.

---

### Impact Explanation

- The specific entropy request is permanently stuck in contract storage and can never be fulfilled or cleared.
- The requester never receives their random number; the fee they paid is effectively lost.
- The provider's off-chain fulfillment service (Fortuna) will repeatedly attempt and fail to fulfill the request, consuming gas on every retry.
- Unlike the new path, there is no `CALLBACK_FAILED` recovery state and no alternative fulfillment route for the legacy path.

---

### Likelihood Explanation

- Any provider whose `defaultGasLimit` is still `0` (the default) is vulnerable. This includes all providers registered before `setDefaultGasLimit` was introduced and any provider that has not yet opted in.
- The attacker only needs to deploy a contract with a reverting `_entropyCallback` and call `requestWithCallback` (or `requestV2` with `gasLimit=0`) against such a provider. Both are permissionless, unprivileged operations.
- No special access, leaked keys, or governance majority is required. [5](#0-4) 

---

### Recommendation

1. **Wrap the legacy callback in a try/catch** (or `excessivelySafeCall`) so that a reverting callback does not revert the entire `revealWithCallback` transaction.
2. **Require `defaultGasLimit > 0` for all providers** (or migrate all existing providers to the new path) so the legacy branch is unreachable.
3. **Add a `clearRequest` escape hatch** for permanently stuck legacy-path requests, callable by the provider or an admin, so storage can be reclaimed even when the callback is broken.

---

### Proof of Concept

```solidity
// Malicious requester contract
contract MaliciousConsumer is IEntropyConsumer {
    address entropy;
    constructor(address _entropy) { entropy = _entropy; }

    function getEntropy() internal view override returns (address) { return entropy; }

    // Always reverts — permanently blocks revealWithCallback
    function entropyCallback(uint64, address, bytes32) internal override {
        revert("blocked");
    }

    function request(address provider, bytes32 userRandom) external payable returns (uint64) {
        // Provider must have defaultGasLimit == 0 (the default)
        uint256 fee = IEntropy(entropy).getFee(provider);
        return IEntropy(entropy).requestWithCallback{value: fee}(provider, userRandom);
    }
}
```

**Steps:**
1. Confirm target provider has `defaultGasLimit == 0` via `getProviderInfoV2(provider).defaultGasLimit`.
2. Deploy `MaliciousConsumer` and call `request(provider, userRandom)` with sufficient fee.
3. The request is stored with `gasLimit10k = 0` (legacy path).
4. Provider calls `revealWithCallback(provider, seqNum, userContrib, providerContrib)`.
5. The call reaches line 676, `_entropyCallback` reverts, the entire tx reverts.
6. The request remains active indefinitely. No on-chain path exists to clear it. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L346-356)
```text
    function requestWithCallback(
        address provider,
        bytes32 userContribution
    ) public payable override returns (uint64) {
        return
            requestV2(
                provider,
                userContribution,
                0 // Passing 0 will assign the request the provider's default gas limit
            );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-577)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L663-681)
```text
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L921-934)
```text
    function setDefaultGasLimit(uint32 gasLimit) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];
        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        // Check that we can round the gas limit into the 10k gas. This reverts
        // if the provided value exceeds the max.
        roundTo10kGas(gasLimit);

        uint32 oldGasLimit = provider.defaultGasLimit;
        provider.defaultGasLimit = gasLimit;
```
