### Title
Untyped Payload Signing in `PythLazer.verifyUpdate` Enables Cross-Chain Signature Replay — (`lazer/contracts/evm/src/PythLazer.sol`)

### Summary

`PythLazer.sol::verifyUpdate()` computes the signed hash as `keccak256(payload)` over raw application bytes with no chain ID, no contract address, and no EIP-712 domain separator. Because the same Pyth-controlled trusted signer key is used across every EVM deployment of `PythLazer`, a valid Lazer price-update signature produced for one chain is cryptographically valid on every other EVM chain where `PythLazer` is deployed.

### Finding Description

In `verifyUpdate`, the verification hash is built as:

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

The `payload` bytes contain only a `PAYLOAD_FORMAT_MAGIC`, a timestamp, a channel byte, and feed data. [2](#0-1) 

None of these fields encode the destination chain ID or the `PythLazer` contract address. The `EVM_FORMAT_MAGIC` constant (`706910618`) is identical across all EVM deployments, so it provides no chain binding. [3](#0-2) 

The trusted-signer registry is keyed only by address and expiry; there is no per-chain signer set. [4](#0-3) 

The Sui contract has the same structural issue: `verify_le_ecdsa_message` calls `secp256k1_ecrecover(signature, payload, 0)` directly over the raw payload bytes with no Sui-specific domain context. [5](#0-4) 

### Impact Explanation

An unprivileged relayer or attacker who observes a valid `verifyUpdate` call on chain A (e.g., Ethereum mainnet) can extract the `update` bytes and submit them verbatim to `PythLazer` on chain B (e.g., Arbitrum, Optimism, BNB Chain). The ECDSA recovery will succeed because the trusted signer key, the payload bytes, and the hash are identical. Concrete harm scenarios:

1. **Testnet → mainnet replay**: If the same signer key is registered on both a testnet and mainnet `PythLazer`, a testnet price update (which may carry a manipulated or stale price) can be replayed on mainnet within the same timestamp window.
2. **Stale-price injection across chains**: An attacker can delay submission of a captured update to a chain where the price has since moved, injecting a stale price that downstream DeFi protocols may accept if their staleness check is loose.
3. **Multiple `PythLazer` instances on the same chain**: If two `PythLazer` proxies exist on the same chain (e.g., a v1 and a v2 deployment), a signature intended for one is valid for the other.

### Likelihood Explanation

`PythLazer` is already deployed on multiple EVM chains. The update bytes are publicly observable on-chain. Any party can copy the calldata from one chain's transaction and replay it on another. No privileged access is required; the only constraint is paying the `verification_fee` (currently 1 wei). [6](#0-5) 

### Recommendation

Bind the signed hash to the deployment context by including the chain ID and contract address in the hashed data, following EIP-712:

```solidity
bytes32 domainSeparator = keccak256(abi.encode(
    keccak256("EIP712Domain(string name,uint256 chainId,address verifyingContract)"),
    keccak256("PythLazer"),
    block.chainid,
    address(this)
));
bytes32 structHash = keccak256(abi.encode(
    keccak256("LazerUpdate(bytes payload)"),
    keccak256(payload)
));
bytes32 hash = keccak256(abi.encodePacked("\x19\x01", domainSeparator, structHash));
```

Alternatively, include `block.chainid` and `address(this)` directly inside the payload that the off-chain signer signs, and verify those fields on-chain before accepting the signature.

### Proof of Concept

1. Deploy `PythLazer` on Ethereum mainnet (chain 1) and Arbitrum (chain 42161) with the same trusted signer.
2. Submit a valid Lazer update to Ethereum mainnet; record the `update` calldata bytes from the transaction.
3. Submit the identical `update` bytes to the Arbitrum `PythLazer.verifyUpdate` with `msg.value >= verification_fee`.
4. Observe that `verifyUpdate` returns successfully with the same `signer` address — the signature is accepted on Arbitrum despite having been produced for Ethereum, because `keccak256(payload)` is chain-agnostic and the trusted signer is shared. [7](#0-6)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

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

**File:** lazer/contracts/evm/test/PythLazer.t.sol (L83-95)
```text
        bytes memory payload = abi.encodePacked(
            uint32(2479346549), // PAYLOAD_FORMAT_MAGIC
            timestamp,
            uint8(channel),
            uint8(feedsData.length)
        );

        for (uint256 i = 0; i < feedsData.length; i++) {
            payload = bytes.concat(payload, feedsData[i]);
        }

        return payload;
    }
```

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L43-63)
```text
public(package) fun verify_le_ecdsa_message(
    state: &State,
    clock: &Clock,
    signature: &vector<u8>,
    payload: &vector<u8>,
) {
    let current_cap = state.current_cap();

    // 0 stands for keccak256 hash
    let pubkey = secp256k1_ecrecover(signature, payload, 0);

    // Check if the recovered pubkey is in the trusted signers list
    let trusted_signers = state.trusted_signers(&current_cap);
    let mut maybe_idx = trusted_signers.find_index!(|signer|
        signer.public_key() == &pubkey
    );

    assert!(maybe_idx.is_some(), ESignerNotTrusted);
    let idx = maybe_idx.extract();
    let expires_at_ms = trusted_signers[idx].expires_at_ms();
    assert!(clock.timestamp_ms() < expires_at_ms, ESignerExpired);
```
