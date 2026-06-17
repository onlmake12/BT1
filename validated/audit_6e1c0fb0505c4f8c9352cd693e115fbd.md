### Title
Missing `providerToCredit` Zero-Address Validation Allows Permanent Fee Locking - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.executeCallback()` accepts a caller-supplied `providerToCredit` address and credits provider fees to it without validating that it is non-zero. After the exclusivity period expires, any unprivileged relayer can call `executeCallback` with `providerToCredit = address(0)`, permanently locking the provider's earned fees in an inaccessible mapping slot.

### Finding Description
In `Echo.executeCallback()`, the `providerToCredit` parameter is only constrained during the exclusivity window:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

After the exclusivity period, no validation is applied to `providerToCredit`. The fee credit then unconditionally writes to the mapping:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

If `providerToCredit` is `address(0)`, the fees accumulate in `_state.providers[address(0)].accruedFeesInWei`. No withdrawal path exists for this slot: `withdraw()` uses `msg.sender`, and `withdrawAsFeeManager()` requires `msg.sender == _state.providers[address(0)].feeManager`, which is also the zero address and therefore unreachable. The funds are permanently locked. [1](#0-0) [2](#0-1) 

### Impact Explanation
The provider who fulfilled the original request loses all fees for that request. The fees are credited to `_state.providers[address(0)]` and can never be withdrawn. The consumer's callback still executes successfully, so the user is unaffected. The financial loss is bounded to the provider fee for each exploited request (`req.fee + msg.value - pythFee`). [3](#0-2) [4](#0-3) 

### Likelihood Explanation
The attack is reachable by any unprivileged relayer after `exclusivityPeriodSeconds` elapses. The attacker only needs valid `updateData` and `priceIds` (publicly available from the Pyth network) and must call `executeCallback` before the legitimate provider does. The exclusivity period creates a race condition window that the attacker can exploit. No privileged access is required. [5](#0-4) 

### Recommendation
Add a zero-address check for `providerToCredit` at the top of `executeCallback`, and optionally also require that the address is a registered provider:

```solidity
require(providerToCredit != address(0), "providerToCredit is zero address");
require(_state.providers[providerToCredit].isRegistered, "providerToCredit not registered");
``` [6](#0-5) 

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback(provider, publishTime, priceIds, gasLimit)` paying the required fee. `req.fee` is set to `msg.value - pythFeeInWei`.
2. The exclusivity period (`exclusivityPeriodSeconds`) elapses without the assigned provider fulfilling the request.
3. Attacker calls:
   ```solidity
   echo.executeCallback(
       address(0),          // providerToCredit = zero address
       sequenceNumber,
       validUpdateData,     // obtained from Pyth network
       priceIds
   );
   ```
4. The exclusivity check is skipped (period expired). `providerToCredit == address(0)` passes unchecked.
5. `_state.providers[address(0)].accruedFeesInWei` is incremented by `req.fee - pythFee`.
6. The consumer's `_echoCallback` is still invoked successfully.
7. The legitimate provider's fees are permanently locked; neither `withdraw()` nor `withdrawAsFeeManager()` can recover them from `address(0)`. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-122)
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-298)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L38-39)
```text
        uint96 feePerGasInWei;
        uint128 accruedFeesInWei;
```
