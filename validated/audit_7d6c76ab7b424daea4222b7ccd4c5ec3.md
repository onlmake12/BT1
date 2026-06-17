### Title
Entropy `revealWithCallback` Legacy Path Permanently Locks User Fees When Requester Contract Lacks `IEntropyConsumer` - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains two execution paths. The legacy path (active when `req.gasLimit10k == 0`) invokes `IEntropyConsumer(callAddress)._entropyCallback(...)` without any try/catch protection. If the requester is a contract that does not implement `IEntropyConsumer`, the bare call reverts, rolling back the entire transaction including the preceding `clearRequest`. The request becomes permanently unfulfillable and the user's paid fee is irretrievably locked in the contract.

### Finding Description

`requestWithCallback` (and `requestV2`) record `msg.sender` as `req.requester` with no verification that the caller implements `IEntropyConsumer`: [1](#0-0) 

When the provider later calls `revealWithCallback`, the code branches on `req.gasLimit10k`: [2](#0-1) 

The **new path** (`gasLimit10k != 0`) uses `excessivelySafeCall`, which catches all reverts and transitions the request to `CALLBACK_FAILED`, allowing recovery: [3](#0-2) 

The **legacy path** (`gasLimit10k == 0`) first clears the request, then makes a bare, unguarded call: [4](#0-3) 

If `_entropyCallback` reverts (because the requester contract does not implement `IEntropyConsumer`), the EVM unwinds the entire transaction, reverting `clearRequest` as well. The request is restored to active state, but every subsequent `revealWithCallback` attempt will identically revert. There is no `cancelRequest` escape hatch, so the fee paid at request time is permanently trapped.

The legacy path is activated whenever `providerInfo.defaultGasLimit == 0`: [5](#0-4) 

### Impact Explanation

Any user who calls `requestWithCallback` or `requestV2` from a contract that does not implement `IEntropyConsumer._entropyCallback`, against a provider whose `defaultGasLimit` is `0`, will have their fee permanently locked. The request slot is also consumed from the provider's sequence, wasting entropy capacity. There is no administrative or user-facing recovery path.

### Likelihood Explanation

The legacy path remains live for any provider registered before the `defaultGasLimit` feature was introduced (or any provider that explicitly leaves it at `0`). A user integrating Entropy via a contract that omits the `IEntropyConsumer` interface — a common mistake given the interface is not enforced at request time — will silently lock their funds. The entry point (`requestWithCallback`) is fully permissionless and requires no privileged role.

### Recommendation

Wrap the legacy callback in a try/catch block, mirroring the new path's resilience:

```solidity
// Legacy path (gasLimit10k == 0)
if (len != 0) {
    try IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    ) {} catch {
        emit CallbackFailed(provider, callAddress, sequenceNumber, ...);
    }
}
```

This ensures `clearRequest` is never rolled back due to a misbehaving or non-conforming requester contract, matching the safety guarantee already provided by the new path.

### Proof of Concept

1. Provider registers with `feeInWei > 0` and `defaultGasLimit == 0` (legacy registration).
2. A contract `VictimContract` (which does **not** implement `IEntropyConsumer`) calls `requestWithCallback{value: fee}(provider, userContribution)`, paying the required fee. `req.gasLimit10k` is set to `0`.
3. Provider calls `revealWithCallback(provider, seqNum, userContribution, providerContribution)`.
4. Execution enters the legacy `else` branch; `clearRequest` executes, then `IEntropyConsumer(VictimContract)._entropyCallback(...)` is called.
5. The call reverts (no matching selector). The entire transaction reverts, restoring the request.
6. Every subsequent `revealWithCallback` attempt identically reverts.
7. `VictimContract`'s fee is permanently locked; the sequence slot is consumed. [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-578)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L582-596)
```text
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-702)
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
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());

            emit RevealedWithCallback(
                reqV1,
                userContribution,
                providerContribution,
                randomNumber
            );
            emit EntropyEventsV2.Revealed(
                provider,
                callAddress,
                sequenceNumber,
                randomNumber,
                userContribution,
                providerContribution,
                false,
                bytes(""),
                gasUsed,
                bytes("")
            );
        }
```
