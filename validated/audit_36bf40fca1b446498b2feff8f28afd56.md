### Title
`PythLazer.verifyUpdate` Uses `.transfer()` for ETH Refund, Causing DoS for Contract Callers Without `receive` Function — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` refunds excess ETH to `msg.sender` using the low-level `.transfer()` primitive. If `msg.sender` is a smart contract that lacks a `receive` or `fallback payable` function — or whose `receive` function consumes more than the 2300 gas stipend forwarded by `.transfer()` — the refund reverts, causing the entire `verifyUpdate` call to revert. Any contract-based Lazer consumer that forwards user-supplied `msg.value` into `verifyUpdate` is therefore permanently DoS-able.

---

### Finding Description

In `PythLazer.verifyUpdate`:

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    // Require fee and refund excess
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);  // ← vulnerable
    }
    ...
}
``` [1](#0-0) 

`.transfer()` forwards exactly 2300 gas to the recipient. Per the Solidity documentation, if the recipient is a contract with no `receive` or `fallback payable` function, the transfer throws and the entire call reverts. Since EIP-1884 (Istanbul), even a minimal `receive` function that emits an event or writes to storage can exceed 2300 gas.

The official Pyth Lazer EVM integration guide shows the canonical consumer pattern:

```solidity
function updatePrice(bytes calldata priceUpdate) public payable {
  uint256 verification_fee = pythLazer.verification_fee();
  (bytes calldata payload, ) = verifyUpdate{ value: verification_fee }(update);
``` [2](#0-1) 

A common and natural deviation from this pattern is for a consumer contract to forward `msg.value` directly (e.g., `verifyUpdate{value: msg.value}(update)`). Whenever `msg.value > verification_fee`, the refund path fires. If the consumer contract has no `receive` function, the entire transaction reverts.

---

### Impact Explanation

Any smart contract that integrates with `PythLazer.verifyUpdate` and sends more ETH than `verification_fee` — whether by design (passing through user-supplied ETH) or by accident (fee changed between query and execution) — will have its call unconditionally revert if it lacks a `receive` function. This is a **permanent, externally-triggerable DoS** on the consumer contract's price-update path. Because `verifyUpdate` is the sole on-chain verification entry point for Lazer price data on EVM, affected consumers lose access to all Lazer price feeds.

---

### Likelihood Explanation

The likelihood is **medium**. The pattern of forwarding `msg.value` into a downstream payable call is extremely common in DeFi. The official documentation example queries the fee first and sends exactly that amount, but:

1. The `verification_fee` can be changed by the owner at any time (it is a mutable state variable), so a consumer that cached the fee or computed it in the same block as a fee change will send the wrong amount.
2. Any consumer contract that passes `msg.value` through (a natural pattern when the consumer itself is payable) will trigger the refund path whenever a user overpays.
3. The consumer contract need not be malicious — it simply needs to lack a `receive` function, which is the default for most contracts. [3](#0-2) 

---

### Recommendation

Replace `.transfer()` with a low-level `.call{value: ...}("")` and check the return value, or use a pull-payment pattern. The safest fix is:

```solidity
if (msg.value > verification_fee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(ok, "ETH refund failed");
}
```

Alternatively, require callers to send exactly `verification_fee` (no excess), eliminating the refund path entirely.

---

### Proof of Concept

1. Deploy a consumer contract with no `receive` function:

```solidity
contract LazerConsumer {
    PythLazer pythLazer;
    constructor(address _lazer) { pythLazer = PythLazer(_lazer); }

    // No receive() function

    function updatePrice(bytes calldata update) external payable {
        // Forwards all msg.value — common pattern
        (bytes calldata payload, ) = pythLazer.verifyUpdate{value: msg.value}(update);
        // ... process payload
    }
}
```

2. Call `updatePrice` with `msg.value = verification_fee + 1 wei`.
3. Inside `verifyUpdate`, `msg.value > verification_fee` is true, so `payable(msg.sender).transfer(1)` is executed.
4. `LazerConsumer` has no `receive` function → `.transfer()` reverts.
5. The entire `updatePrice` call reverts, permanently blocking price updates for this consumer. [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L10-10)
```text
    uint256 public verification_fee;
```

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

**File:** apps/developer-hub/content/docs/price-feeds/pro/integrate-as-consumer/evm.mdx (L63-67)
```text
function updatePrice(bytes calldata priceUpdate) public payable {
  uint256 verification_fee = pythLazer.verification_fee();
  (bytes calldata payload, ) = verifyUpdate{ value: verification_fee }(update);
  //...
}
```
