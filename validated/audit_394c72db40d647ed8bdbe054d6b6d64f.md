### Title
Entropy `revealWithCallback` Legacy Flow Propagates Callback Reverts, Enabling Selective Abort of Unfavorable Random Numbers - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

When a provider has `defaultGasLimit == 0`, the `revealWithCallback` function uses a legacy execution path that calls `_entropyCallback` directly without any error handling. A malicious user can deploy a contract whose callback selectively reverts based on the received random number value, effectively aborting unfavorable outcomes and retrying until a favorable random number is obtained — breaking the fairness guarantee of the Entropy protocol.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` has two execution paths gated on `req.gasLimit10k`:

**New flow** (`gasLimit10k != 0`): Uses `excessivelySafeCall` to catch callback reverts and moves the request to `CALLBACK_FAILED` state, preserving the reveal.

**Legacy flow** (`gasLimit10k == 0`): Calls `_entropyCallback` directly with no error handling. [1](#0-0) 

The legacy path is activated when `providerInfo.defaultGasLimit == 0` at request time: [2](#0-1) 

In the legacy path, `clearRequest` is called **before** the callback. If the callback reverts, the entire transaction reverts — including `clearRequest` — leaving the request active: [3](#0-2) 

The callback receives `randomNumber` as an argument. A malicious contract can inspect this value and revert if it is unfavorable, causing `revealWithCallback` to revert and the request to remain open. The user then submits a new request and repeats until a favorable number is obtained.

The test suite explicitly confirms this behavior: [4](#0-3) 

The `requestWithCallback` function (which triggers this path) is still callable and routes through `requestV2` with `gasLimit=0`: [5](#0-4) 

---

### Impact Explanation

Any application using Entropy for randomness (lotteries, NFT minting, games) with a provider whose `defaultGasLimit == 0` is vulnerable. An attacker can:

1. Deploy a contract whose `_entropyCallback` reverts if `uint256(randomNumber) % N < threshold` (i.e., the outcome is unfavorable).
2. Call `requestWithCallback` repeatedly, paying a fee each time.
3. Each time the provider calls `revealWithCallback` and the outcome is unfavorable, the transaction reverts and the request remains open.
4. The attacker retries with a new request until a favorable random number is delivered.

This breaks the core fairness guarantee of the Entropy protocol: that neither party can selectively abort unfavorable outcomes.

---

### Likelihood Explanation

`defaultGasLimit == 0` is the **default state** for any provider that has not explicitly called `setDefaultGasLimit`. The default provider set during contract initialization may also have `defaultGasLimit == 0`. Any consumer using the legacy `requestWithCallback` API against such a provider is exposed. The `requestWithCallback` function is still present and callable (though marked deprecated in the interface docs): [6](#0-5) 

---

### Recommendation

Add error handling to the legacy flow in `revealWithCallback` (lines 661–702) to catch callback reverts, analogous to the `excessivelySafeCall` pattern used in the new flow. Alternatively, enforce that all providers must set a non-zero `defaultGasLimit` before accepting callback requests, eliminating the legacy path entirely.

---

### Proof of Concept

```solidity
contract SelectiveAbortConsumer is IEntropyConsumer {
    IEntropy public entropy;
    address public provider; // provider with defaultGasLimit == 0

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }

    function requestRandom() external payable {
        uint128 fee = entropy.getFee(provider);
        entropy.requestWithCallback{value: fee}(provider, bytes32(block.timestamp));
    }

    // Called by revealWithCallback in the legacy flow (gasLimit10k == 0)
    function _entropyCallback(
        uint64 /*sequenceNumber*/,
        address /*provider*/,
        bytes32 randomNumber
    ) internal override {
        // Revert if outcome is unfavorable (e.g., won't win a 1-in-10 lottery)
        require(uint256(randomNumber) % 10 == 0, "Unfavorable: aborting");
        // Only reaches here ~10% of the time — favorable outcome accepted
    }
}
```

**Steps:**
1. Deploy `SelectiveAbortConsumer` pointing to a provider with `defaultGasLimit == 0`.
2. Call `requestRandom()` — pays fee, creates request with `gasLimit10k = 0`.
3. Provider's off-chain service calls `revealWithCallback`.
4. `_entropyCallback` receives the random number; if unfavorable, it reverts.
5. `revealWithCallback` reverts entirely (including `clearRequest`), leaving the request active.
6. Attacker calls `requestRandom()` again with a new sequence number.
7. Repeat until `uint256(randomNumber) % 10 == 0` — attacker receives a guaranteed favorable outcome at the cost of ~10 fees on average.

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

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L999-1016)
```text
    function testRequestWithCallbackAndRevealWithCallbackFailing() public {
        bytes32 userRandomNumber = bytes32(uint(42));
        uint fee = random.getFee(provider1);
        EntropyConsumer consumer = new EntropyConsumer(address(random), true);
        vm.deal(address(consumer), fee);
        vm.startPrank(address(consumer));
        uint64 assignedSequenceNumber = random.requestWithCallback{value: fee}(
            provider1,
            userRandomNumber
        );

        vm.expectRevert();
        random.revealWithCallback(
            provider1,
            assignedSequenceNumber,
            userRandomNumber,
            provider1Proofs[assignedSequenceNumber]
        );
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L54-71)
```text
    // Request a random number. The method expects the provider address and a secret random number
    // in the arguments. It returns a sequence number.
    //
    // DEPRECATED: This method is deprecated. Please use requestV2 from the IEntropyV2 interface instead,
    // which provides better callback handling and gas limit control.
    //
    // The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    // The `entropyCallback` method on that interface will receive a callback with the generated random number.
    // `entropyCallback` will be run with the provider's default gas limit (see `getProviderInfo(provider).defaultGasLimit`).
    // If your callback needs additional gas, please use the function `requestv2` from `IEntropyV2` interface
    // with gasLimit as the input parameter.
    //
    // This method will revert unless the caller provides a sufficient fee (at least `getFee(provider)`) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function requestWithCallback(
        address provider,
        bytes32 userRandomNumber
    ) external payable returns (uint64 assignedSequenceNumber);
```
