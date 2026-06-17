### Title
Unchecked `providerToCredit` After Exclusivity Period Allows Fee Theft — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.executeCallback` enforces that only the assigned provider can be credited during the exclusivity window, but applies **no restriction on `providerToCredit`** after that window expires. Any unprivileged caller can register as a provider, wait for the exclusivity period to lapse, then call `executeCallback` with their own address as `providerToCredit`, redirecting the entire request fee away from the legitimate provider.

### Finding Description
`executeCallback` accepts a caller-supplied `providerToCredit` parameter and credits `req.fee + msg.value - pythFee` to `_state.providers[providerToCredit].accruedFeesInWei`. [1](#0-0) 

The only guard is:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
``` [1](#0-0) 

Once `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`, the check is entirely absent. There is no subsequent `require(providerToCredit == req.provider, ...)` or equivalent. The fee is unconditionally credited to the attacker-supplied address: [2](#0-1) 

### Impact Explanation
A malicious actor can steal 100% of the provider fee for any unfulfilled request after the exclusivity period:

1. Call `registerProvider(...)` to become a registered provider.
2. Call `setFeeManager(attacker_address)` to set themselves as their own fee manager.
3. After `req.publishTime + exclusivityPeriodSeconds` elapses, call `executeCallback(attacker_address, sequenceNumber, validUpdateData, priceIds)`.
4. Call `withdrawAsFeeManager(attacker_address, amount)` to extract the stolen fees. [3](#0-2) 

The legitimate provider loses their entire fee for the request. The requester's callback is still executed, so the requester is unaffected, but the provider's economic incentive is destroyed.

### Likelihood Explanation
The exclusivity period is a configurable admin parameter. Any request that the legitimate provider is slow to fulfill (network congestion, downtime, or deliberate delay) becomes exploitable. MEV bots monitoring the mempool can front-run the provider's own `executeCallback` transaction after the exclusivity window. The attack requires no privileged access — only a call to `registerProvider` (permissionless) and possession of valid `updateData` (publicly available from Pyth's price service). [4](#0-3) 

### Recommendation
After the exclusivity period, restrict `providerToCredit` to `req.provider` unless an explicit penalty/redistribution mechanism is intentionally designed. The simplest fix:

```solidity
require(
    providerToCredit == req.provider,
    "providerToCredit must be the assigned provider"
);
```

If the intent is to allow third-party fulfillment after the exclusivity period (to prevent provider censorship), the fee should still be credited to `req.provider`, not to the arbitrary `providerToCredit` address. The caller's incentive can be a separate, smaller bounty.

### Proof of Concept
```
1. Attacker calls registerProvider(0, 0, 0)
2. Attacker calls setFeeManager(attacker_address)
3. User calls requestPriceUpdatesWithCallback(legitimateProvider, T, priceIds, gasLimit)
   → req.fee = X wei, req.provider = legitimateProvider
4. block.timestamp reaches T + exclusivityPeriodSeconds
5. Attacker calls executeCallback(attacker_address, sequenceNumber, updateData, priceIds)
   → _state.providers[attacker_address].accruedFeesInWei += X
   → legitimateProvider receives 0
6. Attacker calls withdrawAsFeeManager(attacker_address, X)
   → X wei transferred to attacker
``` [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-164)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-378)
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
