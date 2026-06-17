### Title
Lazer Price Update Signature Replayable Across EVM Chains - (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate()` computes the signed hash as `keccak256(payload)` where `payload` contains no chain-specific binding (no `block.chainid`, no contract address). The `EVM_FORMAT_MAGIC` constant is identical across every EVM deployment. A valid signed Lazer price update from one EVM chain can be submitted to `verifyUpdate()` on any other EVM chain and will pass signature verification.

### Finding Description
In `PythLazer.verifyUpdate()`, the verification logic is:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `hash` is purely `keccak256(payload)`. The payload contains a timestamp, a channel identifier, and price feed data — none of which are chain-specific. The `EVM_FORMAT_MAGIC` (`706910618`) is a hardcoded constant that is identical across all EVM deployments: [2](#0-1) 

There is no `block.chainid`, no contract address, and no domain separator included in the signed message. The `PythLazer` contract is deployed on multiple EVM chains (Ethereum, Arbitrum, Polygon, BNB, Optimism, etc.) with the same trusted signers. Because the signed hash is chain-agnostic, a signature valid on chain A is equally valid on chain B.

By contrast, the Sui Lazer contract uses a different magic number (`UPDATE_MESSAGE_MAGIC = 1296547300`) and a different signature scheme (secp256k1 keccak256 vs. Solana's Ed25519), which prevents cross-ecosystem replay. But within the EVM ecosystem, no such binding exists. [3](#0-2) 

### Impact Explanation
An attacker who observes a valid signed Lazer price update on chain A can submit it to `PythLazer.verifyUpdate()` on chain B. The call will succeed and return the payload and signer address as if the update were freshly signed for chain B. Concretely:

- **Timestamp manipulation**: An attacker can replay a price update that is slightly stale (e.g., 3–4 seconds old from chain A) onto chain B, bypassing freshness windows enforced by consumer contracts that rely on the payload timestamp.
- **Stale price injection during volatility**: During rapid price movements, a valid but slightly older price from chain A can be injected into chain B's DeFi protocols (lending, perpetuals, options) that use Pyth Lazer prices, enabling favorable liquidations or borrows against a manipulated price.
- **Channel confusion**: A payload signed for a `real_time` channel on chain A can be submitted as a `fixed_rate@200ms` update on chain B; the channel field is in the payload but is not validated by `verifyUpdate()` itself — consumers must parse it manually.

### Likelihood Explanation
The `PythLazer` contract is deployed on multiple EVM chains with the same trusted signer set. Any user who can observe on-chain transactions (or the Lazer WebSocket stream) can obtain valid signed payloads. Submitting them to a different chain requires only a standard transaction. The attack is permissionless and requires no privileged access. The practical window is narrow (price data is global and timestamps are close across chains), but during high-volatility periods the window is sufficient to cause harm in DeFi integrations.

### Recommendation
Include a domain separator in the signed message that binds the signature to the specific chain and contract address. The hash should be computed as:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

Alternatively, adopt EIP-712 structured signing with a proper `DOMAIN_SEPARATOR` that includes `chainId` and `verifyingContract`. The Lazer off-chain signer infrastructure must be updated to include these fields when producing signatures for EVM targets.

### Proof of Concept
1. Deploy `PythLazer` on Ethereum (chain 1) and Arbitrum (chain 42161) with the same trusted signer.
2. Call `verifyUpdate{value: fee}(update)` on Ethereum with a valid signed update; record the `update` bytes.
3. Submit the identical `update` bytes to `verifyUpdate{value: fee}(update)` on Arbitrum.
4. The call succeeds: `isValidSigner(signer)` returns `true` because the same trusted signer is registered, and `keccak256(payload)` produces the same hash regardless of chain.
5. The Arbitrum consumer contract receives the payload and signer address as if the update were freshly produced for Arbitrum, while the payload timestamp may be several seconds stale relative to Arbitrum's current block time. [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-106)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }

        if (update.length < 71) {
            revert("input too short");
        }
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
        }
        uint16 payload_len = uint16(bytes2(update[69:71]));
        if (update.length < 71 + payload_len) {
            revert("input too short");
        }
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L13-15)
```text
const SECP256K1_SIG_LEN: u32 = 65;
const UPDATE_MESSAGE_MAGIC: u32 = 1296547300;
const PAYLOAD_MAGIC: u32 = 2479346549;
```
