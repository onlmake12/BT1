### Title
Excess Fee Refund Sent to `msg.sender` Instead of Actual Payer in `verifyUpdate` - (`lazer/contracts/evm/src/PythLazer.sol`)

### Summary

`PythLazer.verifyUpdate()` refunds excess ETH to `msg.sender`. When called through an intermediary contract (the expected integration pattern for Lazer consumers), `msg.sender` is the intermediary contract, not the user who originally provided the ETH. Excess fees are permanently misdirected to the intermediary unless it has an independent withdrawal mechanism.

### Finding Description

In `PythLazer.sol`, the `verifyUpdate` function collects a `verification_fee` and refunds any overpayment to `msg.sender`:

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    // Require fee and refund excess
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);
    }
    ...
}
``` [1](#0-0) 

PythLazer is designed to be integrated into consumer contracts that call `verifyUpdate` on behalf of end users. In that call chain:

- `tx.origin` = the EOA user who sent ETH
- `msg.sender` = the consumer/intermediary contract

The refund is sent to the consumer contract, not the user. The user's excess ETH is effectively lost unless the consumer contract independently implements a sweep/withdrawal mechanism — which is not required by the interface.

This is structurally identical to the Olas M-19 finding: in that case `msg.sender` was always the `Dispenser` contract; here `msg.sender` is always the consumer contract when Lazer is used as intended.

### Impact Explanation

Any user who overpays `verification_fee` when calling `verifyUpdate` through an intermediary contract loses the excess ETH. The funds are transferred to the intermediary contract's balance. If the intermediary has no ETH withdrawal function (common for non-custodial consumer contracts), the funds are permanently locked. Even if the intermediary does have a withdrawal function, the user has no claim to those funds.

### Likelihood Explanation

Lazer is explicitly designed for consumer contracts to call `verifyUpdate` and use the returned `payload` and `signer`. The `verification_fee` is currently `1 wei` but is configurable by the owner via `initialize`. Any future fee increase, or any user who sends more ETH than the exact fee (e.g., to avoid reverts due to fee changes), triggers the misdirected refund. The integration pattern that causes the bug is the normal, expected usage.

### Recommendation

Replace `msg.sender` with `tx.origin` in the refund path, consistent with the Olas mitigation:

```solidity
if (msg.value > verification_fee) {
    payable(tx.origin).transfer(msg.value - verification_fee);
}
```

Alternatively, reject any `msg.value` that exceeds `verification_fee` (require exact payment), or add an explicit `refundTo` address parameter so callers can specify where excess fees should be returned.

### Proof of Concept

1. Owner sets `verification_fee = 0.01 ether`.
2. User calls `ConsumerContract.checkPrice{value: 0.1 ether}(update)`.
3. `ConsumerContract` calls `PythLazer.verifyUpdate{value: 0.1 ether}(update)`.
4. Inside `verifyUpdate`: `msg.value (0.1 ether) > verification_fee (0.01 ether)`, so `0.09 ether` is transferred to `msg.sender` = `ConsumerContract`.
5. User (`tx.origin`) receives nothing. The `0.09 ether` sits in `ConsumerContract`.
6. If `ConsumerContract` has no ETH withdrawal, the `0.09 ether` is permanently locked. [2](#0-1)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-77)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
