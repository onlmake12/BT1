### Title
Unbounded Gas Forwarding in Legacy `revealWithCallback` Path Enables Gas Bomb and Permanent DoS by Malicious Consumer - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function has two execution paths. The modern path (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` with a bounded gas limit. The legacy path (when `req.gasLimit10k == 0`, triggered when a provider has `defaultGasLimit == 0`) calls `_entropyCallback` with **no gas cap and no try/catch**. A malicious consumer contract can exploit this to either (1) force excessive gas costs on whoever calls `revealWithCallback` (gas bomb), or (2) permanently DoS a specific request by always reverting in the callback.

---

### Finding Description

In `requestHelper`, when `providerInfo.defaultGasLimit == 0`, the request is stored with `req.gasLimit10k = 0`: [1](#0-0) 

In `revealWithCallback`, the branch condition `req.gasLimit10k != 0` gates the safe path. When `gasLimit10k == 0`, execution falls into the `else` branch: [2](#0-1) 

The `else` branch calls `_entropyCallback` with **no gas limit and no error handling**: [3](#0-2) 

Contrast this with the safe path, which uses `excessivelySafeCall` with `uint256(req.gasLimit10k) * TEN_THOUSAND` as an upper bound and catches reverts: [4](#0-3) 

The `revealWithCallback` function is `public` and callable by anyone: [5](#0-4) 

The test suite explicitly acknowledges this behavior — a provider with `defaultGasLimit == 0` "can never cause a callback to fail because it runs out of gas," confirming all gas is forwarded unconditionally: [6](#0-5) 

---

### Impact Explanation

**Gas bomb**: A malicious consumer contract implements `entropyCallback` to consume all available gas (e.g., a tight loop). Whoever calls `revealWithCallback` — typically the provider's keeper service — pays an arbitrarily large gas fee. On high-gas-price chains, this can make fulfillment economically infeasible.

**Permanent DoS**: If the malicious callback always reverts (e.g., `revert("blocked")`), the entire `revealWithCallback` transaction reverts because there is no `try/catch`. The request remains active in storage but can never be fulfilled, permanently locking the request in an unfulfillable state. The user's paid fee is also effectively lost.

---

### Likelihood Explanation

- Any unprivileged user can deploy a malicious `IEntropyConsumer` contract and call `requestWithCallback` against any provider with `defaultGasLimit == 0`.
- The `revealWithCallback` function is `public` with no access control — anyone can trigger it, including the attacker themselves to demonstrate the gas cost.
- Providers that have not yet called `setDefaultGasLimit` (i.e., legacy providers) are affected by default, since `defaultGasLimit` initializes to `0`. [7](#0-6) 

---

### Recommendation

1. Wrap the legacy-path callback in a `try/catch` to prevent a reverting callback from blocking fulfillment.
2. Apply a gas cap (e.g., the provider's `defaultGasLimit` or a protocol-wide maximum) even in the legacy path, mirroring the safe path's use of `excessivelySafeCall`.
3. Alternatively, deprecate the legacy path entirely and require all providers to set a non-zero `defaultGasLimit` before accepting new requests.

---

### Proof of Concept

1. Deploy a provider with `defaultGasLimit == 0` (the default state for any newly registered provider).
2. Deploy a malicious consumer contract:
   ```solidity
   function entropyCallback(uint64, address, bytes32) internal override {
       // Gas bomb: consume all gas
       uint256 i = 0;
       while (true) { i++; }
       // OR: permanent DoS
       revert("blocked");
   }
   ```
3. Call `requestWithCallback(provider, userRandomNumber)` from the malicious consumer. The request is stored with `gasLimit10k == 0`.
4. Call `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. Observe: the transaction either consumes all available gas (gas bomb) or reverts entirely (DoS), with no recovery path for the request. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L541-547)
```text
    // Anyone can call this method to fulfill a request, but the callback will only be made to the original requester.
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-576)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
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

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1801-1808)
```text
        // A provider that hasn't upgraded to the callback failure flow
        // can never cause a callback to fail because it runs out of gas.
        vm.prank(provider1);
        random.setDefaultGasLimit(0);
        assertCallbackResult(0, 190000, true);
        assertCallbackResult(0, 210000, true);
        assertCallbackResult(300000, 290000, true);
        assertCallbackResult(300000, 310000, true);
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L40-42)
```text
        // Default gas limit to use for callbacks.
        uint32 defaultGasLimit;
    }
```
