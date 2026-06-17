### Title
Unvalidated `providerToCredit` in `executeCallback` Allows Fee Theft After Exclusivity Period - (File: target_chains/ethereum/contracts/contracts/echo/Echo.sol)

### Summary

In `Echo.sol`'s `executeCallback`, the caller-supplied `providerToCredit` address is used to credit the full request fee with no validation that it is the legitimate provider for the request once the exclusivity period has elapsed. An attacker who registers as a provider can redirect fees earned by the legitimate provider to their own address.

### Finding Description

`executeCallback` accepts a caller-controlled `providerToCredit` parameter and credits it with `(req.fee + msg.value) - pythFee`:

```solidity
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
    Request storage req = findActiveRequest(sequenceNumber);

    if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
        require(
            providerToCredit == req.provider,
            "Only assigned provider during exclusivity period"
        );
    }
    // ...
    _state.providers[providerToCredit].accruedFeesInWei += SafeCast
        .toUint128((req.fee + msg.value) - pythFee);
``` [1](#0-0) 

The exclusivity check enforces `providerToCredit == req.provider` only while `block.timestamp < req.publishTime + exclusivityPeriodSeconds`. After that window, the check is skipped entirely and `providerToCredit` is accepted as any address. There is no subsequent validation that `providerToCredit` is the legitimate provider stored in `req.provider`, nor even that it is a registered provider. [2](#0-1) 

`registerProvider` is permissionless — any address can register: [3](#0-2) 

`withdrawAsFeeManager` allows the fee manager of any provider to withdraw that provider's accrued fees to `msg.sender`: [4](#0-3) 

### Impact Explanation

A malicious actor can steal the fees that were legitimately earned by the assigned provider of any request. The full `req.fee` (paid by the requester at request time) plus any `msg.value` attached to `executeCallback` is redirected to the attacker's provider account and then withdrawn. The legitimate provider receives nothing for fulfilling the request.

### Likelihood Explanation

The attack requires only:
1. Registering as a provider (permissionless, zero-cost beyond gas).
2. Waiting for the exclusivity period to expire on any active request.
3. Calling `executeCallback` with valid `updateData` and `providerToCredit` set to the attacker's own provider address.

No privileged access, leaked keys, or oracle manipulation is required. Any unprivileged address can execute this. The exclusivity period is a finite window; every request eventually becomes vulnerable.

### Recommendation

After the exclusivity period, restrict `providerToCredit` to registered providers only, or — more strictly — require it to equal `req.provider` at all times (matching the pattern used during the exclusivity window). At minimum, add:

```solidity
require(
    _state.providers[providerToCredit].isRegistered,
    "providerToCredit must be a registered provider"
);
```

A stronger fix mirrors the TON router recommendation: validate that `providerToCredit` is the provider that was actually assigned to the request (`req.provider`), unless a deliberate penalty/reassignment mechanism is intended.

### Proof of Concept

1. **Setup**: Attacker calls `registerProvider(0, 0, 0)` to register `attackerAddr` as a provider. Attacker calls `setFeeManager(attackerAddr)` so they can withdraw.
2. **Wait**: A legitimate user calls `requestPriceUpdatesWithCallback(legitimateProvider, ...)` paying fee `F`. The request is stored with `req.provider = legitimateProvider` and `req.fee = F - pythFee`.
3. **Exploit**: After `exclusivityPeriodSeconds` elapses, attacker calls `executeCallback(attackerAddr, sequenceNumber, updateData, priceIds)`. The check at line 114–121 is skipped. Line 161–162 credits `_state.providers[attackerAddr].accruedFeesInWei += (req.fee + msg.value) - pythFee`.
4. **Drain**: Attacker calls `withdrawAsFeeManager(attackerAddr, amount)`, receiving the stolen fees. `legitimateProvider.accruedFeesInWei` remains zero. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-164)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
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

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-379)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
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
```
