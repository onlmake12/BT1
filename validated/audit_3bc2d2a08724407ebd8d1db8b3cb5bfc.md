### Title
`.transfer()` in `verifyUpdate()` Excess-Fee Refund Causes Permanent DoS for Smart Contract Callers - (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate()` uses Solidity's `.transfer()` to refund excess fee to `msg.sender`. This hard-caps the gas forwarded to 2300, causing an unconditional revert for any smart contract caller (e.g., a multisig wallet, a DeFi protocol, or any contract with a non-trivial `receive`/`fallback`) that overpays the fee. Such callers are permanently unable to use the Lazer price-feed verification path.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate()` function accepts a fee and refunds any excess to the caller:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  line 75-77
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [1](#0-0) 

`address.transfer()` forwards exactly 2300 gas. Any contract whose `receive` or `fallback` function consumes more than 2300 gas (e.g., a Gnosis Safe multisig, any ERC-777 hook, or a contract that emits an event on receipt) will cause this call to revert. Because the refund is inside `verifyUpdate()` itself, the entire price-update verification transaction reverts, making the function completely unusable for that caller.

### Impact Explanation
Smart contract integrators — including multisig-controlled protocols, automated keepers, and any on-chain consumer of Pyth Lazer prices — that send `msg.value > verification_fee` will have every call to `verifyUpdate()` revert. There is no workaround short of sending exactly `verification_fee` to the wei, which is fragile and breaks if `verification_fee` is updated by the owner. This constitutes a denial-of-service against a class of legitimate Lazer users.

### Likelihood Explanation
`verifyUpdate()` is a public, payable, permissionless entry point intended to be called by any Lazer consumer. Smart contract callers are a common and expected integration pattern. Sending a small buffer above the exact fee is standard practice to avoid reverts from fee changes. The likelihood of triggering this is high for any contract-based caller.

### Recommendation
Replace `.transfer()` with OpenZeppelin's `Address.sendValue()`, which forwards all available gas and reverts safely on failure:

```solidity
import "@openzeppelin/contracts/utils/Address.sol";
// ...
if (msg.value > verification_fee) {
    Address.sendValue(payable(msg.sender), msg.value - verification_fee);
}
```

Alternatively, use a low-level call:
```solidity
(bool ok, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
require(ok, "refund failed");
```

### Proof of Concept
1. Deploy a contract `Caller` whose `receive()` function emits an event (costs >2300 gas).
2. From `Caller`, call `PythLazer.verifyUpdate{value: verification_fee + 1}(validUpdate)`.
3. The `.transfer()` at line 76 forwards only 2300 gas to `Caller.receive()`, which runs out of gas.
4. The entire `verifyUpdate()` call reverts.
5. `Caller` can never successfully call `verifyUpdate()` while sending any excess value. [2](#0-1)

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
