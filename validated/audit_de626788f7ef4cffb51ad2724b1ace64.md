### Title
Pyth Oracle Fee Mismatch in `executeCallback` Can Permanently Lock User ETH — (`Echo.sol`)

---

### Summary

In `Echo.sol`, the `executeCallback` function computes the provider credit as `(req.fee + msg.value) - pythFee`. The `req.fee` is stored at request time based on the provider's configured fees, while `pythFee` is computed dynamically at execution time via `pyth.getUpdateFee(updateData)`. If `pythFee > req.fee + msg.value`, the arithmetic underflows and reverts. Because Echo has no cancel or refund mechanism, user ETH becomes permanently locked in the contract.

---

### Finding Description

**At request time** (`requestPriceUpdatesWithCallback`):

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
_state.accruedFeesInWei += _state.pythFeeInWei;
```

`_state.pythFeeInWei` is Echo's own protocol fee (credited to the Echo admin), **not** the Pyth oracle fee. The Pyth oracle fee is expected to be embedded in the provider's configured fees (`providerBaseFee + providerFeedFee + gasFee`). So `req.fee` equals the provider's share of the user's payment, which is supposed to cover the Pyth oracle fee. [1](#0-0) 

**At execution time** (`executeCallback`):

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(...);

_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

`pythFee` is computed dynamically from the live Pyth oracle contract. If the Pyth oracle fee has increased since the provider last set their fees, `pythFee > req.fee + msg.value` and the subtraction underflows, causing the entire transaction to revert. [2](#0-1) 

**No refund mechanism exists.** The only ETH withdrawal paths are:
- `withdrawFees` — admin withdraws `_state.accruedFeesInWei` (Echo protocol fees only)
- `withdrawAsFeeManager` — fee manager withdraws provider accrued fees

Neither path can recover ETH locked in an unfulfillable request. [3](#0-2) 

The `getFee` function documents the design assumption but does not enforce it on-chain:

> "Note: The provider needs to set its fees to include the fee charged by the Pyth contract." [4](#0-3) 

The comment on `executeCallback` itself acknowledges the unresolved design:

> "TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees." [5](#0-4) 

---

### Impact Explanation

If `pythFee > req.fee + msg.value`, `executeCallback` reverts unconditionally. The request remains active but unfulfillable. The user's ETH (locked as `req.fee` in the request struct) has no withdrawal path. The user cannot cancel the request, cannot get a refund, and cannot force the provider to subsidize the fee increase. The ETH is permanently locked unless the contract is upgraded (it is upgradeable via `EchoUpgradeable`), which mitigates but does not eliminate the risk. [6](#0-5) 

---

### Likelihood Explanation

The Pyth oracle fee is set by governance and can increase. The provider's configured fees (`baseFeeInWei`, `feePerFeedInWei`, `feePerGasInWei`) are set once via `registerProvider` or `setProviderFee` and are not automatically updated when the Pyth oracle fee changes. Any governance-driven Pyth fee increase after a request is submitted creates the mismatch. Additionally, a provider who misconfigures their fees (setting them too low to cover the Pyth oracle fee) triggers the same condition immediately. [7](#0-6) 

---

### Recommendation

1. **Add a request cancellation/refund

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-99)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-105)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-162)
```text
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L240-255)
```text
        uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
        // Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
        // Ideally, we would be able to automatically compute the pyth fees from the priceIds, but the
        // fee computation on IPyth assumes it has the full updated data.
        uint96 providerBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-379)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }

    function findActiveRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        req = findRequest(sequenceNumber);

        if (!isActive(req) || req.sequenceNumber != sequenceNumber)
            revert NoSuchRequest();
    }

    function findRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            return req;
        } else {
            req = _state.requestsOverflow[key];
        }
    }

    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
    }

    function allocRequest(
        uint64 sequenceNumber
    ) internal returns (Request storage req) {
        (, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (isActive(req)) {
            (bytes32 reqKey, ) = requestKey(req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
    }

    function isActive(Request memory req) internal pure returns (bool) {
        return req.sequenceNumber != 0;
    }

    function setFeeManager(address manager) external override {
        require(
            _state.providers[msg.sender].isRegistered,
            "Provider not registered"
        );
        address oldFeeManager = _state.providers[msg.sender].feeManager;
        _state.providers[msg.sender].feeManager = manager;
        emit FeeManagerUpdated(msg.sender, oldFeeManager, manager);
    }

    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-426)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }

    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );

        uint96 oldBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 oldFeePerFeed = _state.providers[provider].feePerFeedInWei;
        uint96 oldFeePerGas = _state.providers[provider].feePerGasInWei;
        _state.providers[provider].baseFeeInWei = newBaseFeeInWei;
        _state.providers[provider].feePerFeedInWei = newFeePerFeedInWei;
        _state.providers[provider].feePerGasInWei = newFeePerGasInWei;
        emit ProviderFeeUpdated(
            provider,
            oldBaseFee,
            oldFeePerFeed,
            oldFeePerGas,
            newBaseFeeInWei,
            newFeePerFeedInWei,
            newFeePerGasInWei
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoUpgradeable.sol (L1-5)
```text
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;

import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
```
