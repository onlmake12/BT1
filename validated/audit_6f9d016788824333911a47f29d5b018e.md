### Title
Frontrunning `executeCallback` Allows Attacker to Steal Provider Fees After Exclusivity Period - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

In `Echo.sol`, the `executeCallback` function accepts an unconstrained `providerToCredit` address parameter. After the exclusivity period expires, any caller can invoke `executeCallback` with an arbitrary `providerToCredit`. An attacker monitoring the mempool can frontrun a legitimate provider's `executeCallback` transaction, substituting their own address as `providerToCredit`, thereby stealing the fees that should have been credited to the legitimate provider. The legitimate provider's transaction then reverts because the request has already been cleared.

### Finding Description

`Echo.executeCallback` enforces provider exclusivity only within the window `block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds`. Outside that window, the `providerToCredit` parameter is completely unconstrained:

```solidity
// Echo.sol lines 113–121
if (
    block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

After the exclusivity period, the entire fee (`req.fee + msg.value - pythFee`) is credited to whatever address is passed as `providerToCredit`:

```solidity
// Echo.sol line 161–162
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

There is no check that `providerToCredit` is the caller (`msg.sender`), the original assigned provider, or even a registered provider. An attacker who:
1. Calls `registerProvider(0, 0, 0)` to register with zero fees, and
2. Calls `setFeeManager(attackerAddress)` to set themselves as their own fee manager,

can then frontrun any pending `executeCallback` transaction by copying the `sequenceNumber`, `updateData`, and `priceIds` from the mempool and substituting their own address as `providerToCredit`. The attacker then calls `withdrawAsFeeManager(attackerAddress, amount)` to drain the stolen fees.

The legitimate provider's transaction reverts with `NoSuchRequest` because `clearRequest` has already been called.

### Impact Explanation

- **Provider fee theft**: The attacker receives all fees (`req.fee + msg.value - pythFee`) that were paid by the user and intended for the legitimate provider.
- **DoS on legitimate provider**: The legitimate provider's `executeCallback` transaction reverts after being frontrun, wasting their gas and denying them their earned fees.
- **Severity**: Medium-High. On any EVM chain with a public mempool, this attack is straightforward and repeatable. Every unfulfilled request that passes its exclusivity window is a target.

### Likelihood Explanation

- `executeCallback` is a public, payable function callable by any address.
- Registering as a provider (`registerProvider`) is permissionless.
- Setting a fee manager (`setFeeManager`) is permissionless for registered providers.
- Public mempools on all major EVM chains make pending transactions visible.
- The attacker only needs to copy the calldata and change `providerToCredit` to their own address.
- No privileged access, leaked keys, or governance majority is required.

### Recommendation

Restrict `providerToCredit` after the exclusivity period to `msg.sender` only, removing the ability to redirect fees to an arbitrary address:

```solidity
function executeCallback(
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
    address providerToCredit = msg.sender; // always credit the caller
    ...
}
```

Alternatively, if the intent is to allow a provider to credit a different address, require that `providerToCredit` be the caller or the originally assigned provider:

```solidity
require(
    providerToCredit == msg.sender || providerToCredit == req.provider,
    "Invalid providerToCredit"
);
```

### Proof of Concept

1. Attacker calls `echo.registerProvider(0, 0, 0)` — registers with zero fees.
2. Attacker calls `echo.setFeeManager(attackerAddress)` — sets themselves as fee manager.
3. Legitimate provider prepares and broadcasts `echo.executeCallback(legitimateProvider, seqNum, updateData, priceIds)` after the exclusivity period.
4. Attacker observes the pending transaction in the mempool, copies `seqNum`, `updateData`, `priceIds`, and submits `echo.executeCallback(attackerAddress, seqNum, updateData, priceIds)` with higher gas price.
5. Attacker's transaction is mined first. `_state.providers[attackerAddress].accruedFeesInWei` is credited with the full fee.
6. Legitimate provider's transaction reverts (`NoSuchRequest`).
7. Attacker calls `echo.withdrawAsFeeManager(attackerAddress, stolenAmount)` to withdraw the stolen fees.

**Root cause**: [1](#0-0) 

**Fee credit with unconstrained `providerToCredit`**: [2](#0-1) 

**Permissionless provider registration**: [3](#0-2) 

**Fee manager withdrawal path**: [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-390)
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
```
