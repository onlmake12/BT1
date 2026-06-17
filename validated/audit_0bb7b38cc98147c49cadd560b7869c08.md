### Title
Missing Chain ID in Lazer Update Signature Enables Cross-Chain Replay — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` computes the signed digest as `keccak256(payload)` where the payload contains no chain ID, no contract address, and no domain separator. A valid Lazer price update signed for one EVM chain is therefore cryptographically identical on every other EVM chain where `PythLazer` is deployed, enabling any unprivileged caller to replay a captured update across chains.

---

### Finding Description

In `PythLazer.verifyUpdate`, the verification hash is constructed as:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(hash, ...);
``` [1](#0-0) 

The `payload` bytes are parsed by `PythLazerLib.parsePayloadHeader`, which reads only: a format magic constant, a timestamp, a channel byte, and a feed count — no chain ID, no `block.chainid`, no contract address, and no EIP-712 domain separator. [2](#0-1) 

`PythLazer` is deployed deterministically to the same address on every EVM chain via CreateX `create3`, and the same trusted signers are registered across all deployments. [3](#0-2) 

Because the signed digest is `keccak256(payload)` with no chain-binding context, a signature that is valid on chain A is equally valid on chain B. There is no on-chain mechanism to reject a cross-chain replayed update.

---

### Impact Explanation

Any unprivileged actor who observes a valid `verifyUpdate` call on chain A can immediately re-submit the same `update` bytes on chain B. Downstream integrators that call `verifyUpdate` and act on the returned `payload` (e.g., to update an on-chain price) will accept the replayed data as authentic. In a chain-fork scenario (the exact scenario from the reference report), both fork chains share the same signed updates indefinitely, since neither the payload nor the signature contains any fork-discriminating information. Protocols that rely on Lazer prices for liquidations, collateral valuation, or settlement are exposed to stale or chain-inappropriate price injection.

---

### Likelihood Explanation

`PythLazer` is already deployed on multiple EVM chains using a deterministic address scheme. All transactions on public EVM chains are observable. Any user can copy a calldata blob from chain A's mempool or history and submit it to chain B. No privileged access, leaked key, or social engineering is required. The only natural friction is the `verification_fee` (currently `1 wei`), which is negligible. [4](#0-3) 

---

### Recommendation

Include chain-binding context in the signed digest. The standard approach is EIP-712: compute a domain separator that commits to `block.chainid` and `address(this)`, and prepend it to the payload hash:

```solidity
bytes32 domainSeparator = keccak256(abi.encode(
    keccak256("EIP712Domain(string name,uint256 chainId,address verifyingContract)"),
    keccak256("PythLazer"),
    block.chainid,
    address(this)
));
bytes32 hash = keccak256(abi.encodePacked("\x19\x01", domainSeparator, keccak256(payload)));
```

Alternatively, include `block.chainid` directly inside the serialized payload format so the Lazer signer commits to a specific chain per update. Either approach ensures a signature produced for chain A is rejected on chain B.

---

### Proof of Concept

1. Deploy `PythLazer` on chain A (e.g., Ethereum mainnet) and chain B (e.g., Base) — both at the same deterministic address.
2. A trusted Lazer signer publishes a price update; a valid `verifyUpdate` call is broadcast on chain A with calldata `update`.
3. An attacker copies the exact `update` bytes from chain A's transaction history.
4. The attacker calls `verifyUpdate{value: 1 wei}(update)` on chain B.
5. The call succeeds: `keccak256(payload)` is identical on both chains, `ECDSA.tryRecover` returns the same trusted signer address, and `isValidSigner` passes.
6. The attacker receives the verified `payload` and `signer` return values on chain B, and any downstream contract that trusts this return value accepts the cross-chain replayed price. [5](#0-4)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L26-27)
```text
        verification_fee = 1 wei;
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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L122-136)
```text
        uint32 FORMAT_MAGIC = 2479346549;

        pos = 0;
        uint32 magic = _readBytes4(update, pos);
        pos += 4;
        if (magic != FORMAT_MAGIC) {
            revert("invalid magic");
        }
        timestamp = _readBytes8(update, pos);
        pos += 8;
        channel = PythLazerStructs.Channel(_readBytes1(update, pos));
        pos += 1;
        feedsLen = uint8(_readBytes1(update, pos));
        pos += 1;
    }
```

**File:** lazer/contracts/evm/script/PythLazerDeploy.s.sol (L49-58)
```text
contract PythLazerDeployScript is Script {
    // The address of the wallet calling this script. This is used to protect
    // the deployment addresses from being redeployed by other wallets.
    address constant deployer = 0x78357316239040e19fC823372cC179ca75e64b81;

    // CreateX is a Contract Factory that provides multiple deployment solutions that
    // we use for deterministic deployments of our contract. It is universally deployed
    // at this address and can be deployed if it is not already deployed.
    ICreateX constant createX =
        ICreateX(0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed);
```
