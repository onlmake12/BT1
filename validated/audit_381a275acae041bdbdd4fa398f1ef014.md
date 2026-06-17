### Title
Contract Passing `extcodesize` Check But Not Implementing `IEntropyConsumer` Permanently Bricks Its Request — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the legacy `else` branch of `revealWithCallback` checks only whether the requester is a contract (`extcodesize != 0`) before calling `IEntropyConsumer(callAddress)._entropyCallback(...)` with no error handling. If the requester is a contract that does not implement `_entropyCallback`, the call reverts, rolling back `clearRequest`, and the request is permanently unresolvable.

---

### Finding Description

`revealWithCallback` has two execution paths:

**New path** (lines 574–660): triggered when `req.gasLimit10k != 0 && req.callbackStatus == CALLBACK_NOT_STARTED`. The callback is wrapped in `excessivelySafeCall`, so any revert is caught and the request moves to `CALLBACK_FAILED` state for recovery.

**Old path** (lines 661–702): triggered when `req.gasLimit10k == 0` (which occurs whenever `providerInfo.defaultGasLimit == 0`). The code:

1. Calls `clearRequest(provider, sequenceNumber)` — before the callback.
2. Checks `extcodesize(callAddress) != 0`.
3. If true, calls `IEntropyConsumer(callAddress)._entropyCallback(...)` with **no try/catch**. [1](#0-0) 

The `gasLimit10k == 0` condition is set in `requestHelper` when `providerInfo.defaultGasLimit == 0`: [2](#0-1) 

A contract that calls `requestWithCallback` (or `requestV2(..., 0)`) without inheriting `IEntropyConsumer` passes the `extcodesize` check but has no `_entropyCallback` selector. The low-level call reverts, rolling back `clearRequest`. The request remains active but can never be cleared: every subsequent `revealWithCallback` call hits the same revert. The user's fee was already credited to the provider in `requestHelper` and is unrecoverable. [3](#0-2) 

The `IEntropyConsumer` abstract contract defines `_entropyCallback` as the required external entry point: [4](#0-3) 

---

### Impact Explanation

- The specific request is permanently stuck: `revealWithCallback` always reverts for that `(provider, sequenceNumber)` pair.
- The user's fee (paid at request time) is irrecoverable — it was already credited to the provider and Pyth protocol.
- The request occupies a slot in the 32-slot hash table indefinitely, potentially degrading throughput for that provider.
- No admin or recovery path exists in the old code flow (unlike the new path's `CALLBACK_FAILED` state).

---

### Likelihood Explanation

The old path is active whenever a provider has not set `defaultGasLimit` (i.e., `providerInfo.defaultGasLimit == 0`). Any contract that calls `requestWithCallback` without implementing `IEntropyConsumer` — a realistic developer mistake given the documentation only recommends but does not enforce the interface — triggers the permanent DoS. The entry point (`requestWithCallback`) is permissionless and requires no privileged role. [5](#0-4) 

---

### Recommendation

Wrap the `_entropyCallback` call in the old path with a `try/catch` (or `excessivelySafeCall`) so that a missing or reverting callback does not brick the request. Alternatively, enforce that `providerInfo.defaultGasLimit != 0` for all active providers, eliminating the old path entirely.

---

### Proof of Concept

1. Deploy a provider with `defaultGasLimit == 0` (never called `setDefaultGasLimit`).
2. Deploy a contract `VictimContract` that calls `requestWithCallback` but does **not** inherit `IEntropyConsumer` (no `_entropyCallback` function).
3. `VictimContract` calls `requestWithCallback{value: fee}(provider, userRandomNumber)`. Fee is credited; `req.gasLimit10k == 0`.
4. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. Execution enters the `else` branch. `clearRequest` is called. `extcodesize(VictimContract) != 0` → true. `IEntropyConsumer(VictimContract)._entropyCallback(...)` is called. No such selector → EVM reverts. Entire transaction reverts including `clearRequest`.
6. Request is still active. Repeat step 4 → always reverts. Request is permanently bricked; user fee is lost. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L236-239)
```text
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyConsumer.sol (L8-18)
```text
    function _entropyCallback(
        uint64 sequence,
        address provider,
        bytes32 randomNumber
    ) external {
        address entropy = getEntropy();
        require(entropy != address(0), "Entropy address not set");
        require(msg.sender == entropy, "Only Entropy can call this function");

        entropyCallback(sequence, provider, randomNumber);
    }
```
