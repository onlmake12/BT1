### Title
Missing Domain Separation in `verifyUpdate` Enables Cross-Chain Signature Replay - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` computes the verification hash as a raw `keccak256(payload)` with no chain ID, contract address, or EIP-712 domain separator bound into the signed digest. Because the same trusted Lazer signer key is registered across multiple EVM deployments, any valid signed update observed on one chain can be replayed verbatim on any other EVM chain where that signer is active. This is the direct Pyth analog of the external report's "signing without domain separation" class: the signed data carries no context that ties it to a specific chain or contract instance.

---

### Finding Description

In `PythLazer.sol`, `verifyUpdate` extracts the payload and computes:

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

The signed digest is purely `keccak256(payload)`. The payload itself contains only:
- A 4-byte `PAYLOAD_MAGIC` constant (`2479346549`) — identical on every chain
- A `uint64` timestamp
- A `uint8` channel ID
- Per-feed price data [2](#0-1) 

No chain ID, no contract address, and no EIP-712 domain separator is included anywhere in the signed bytes. The `isValidSigner` check only tests whether the recovered address is in the trusted-signer mapping and has not expired:

```solidity
function isValidSigner(address signer) public view returns (bool) {
    return block.timestamp < trustedSignerToExpiresAtMapping[signer];
}
``` [3](#0-2) 

The same structural absence exists in the Sui contract, where `verify_le_ecdsa_message` calls `secp256k1_ecrecover(signature, payload, 0)` directly on the raw payload bytes with no domain binding: [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker who observes a valid Lazer update transaction on chain A (e.g., Ethereum mainnet) can extract the raw `update` bytes and submit them to `verifyUpdate` on chain B (e.g., Arbitrum, Base, or any other EVM deployment) where the same trusted signer is registered. The call will succeed: the signature is valid because the hash is identical on both chains.

The returned `(payload, signer)` tuple is accepted by the consuming DeFi protocol on chain B as a freshly verified Lazer price. If the attacker selectively replays an update from a moment when prices were favorable (e.g., within the freshness window of the consuming contract), they can feed a stale or manipulated price to any protocol that relies on `verifyUpdate` without independently enforcing chain-specific freshness or provenance. This can cause incorrect liquidations, mispriced options, or other financial losses in protocols consuming Pyth Lazer on EVM chains.

---

### Likelihood Explanation

- The Lazer deployment model explicitly registers the same trusted signer key across all EVM chains (the `updateTrustedSigner` governance flow is chain-agnostic).
- Valid signed updates are publicly observable on-chain or via the Lazer WebSocket stream.
- No special privilege is required: any address can call `verifyUpdate` with a fee of 1 wei.
- The attack requires only copying bytes from one chain's calldata and submitting them on another — a trivially automatable operation.

---

### Recommendation

Bind the signed digest to the specific chain and contract instance by including `block.chainid` and `address(this)` in the hashed data, following EIP-712:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

Alternatively, adopt a full EIP-712 typed-data structure with a domain separator. Apply the equivalent fix to the Sui (`verify_le_ecdsa_message`) and Aptos (`verify_message`) contracts by including a chain/network identifier in the signed message.

---

### Proof of Concept

1. On Ethereum mainnet, call `PythLazer.verifyUpdate{value: 1 wei}(update)` with a live Lazer update. Record the raw `update` calldata bytes.
2. On Arbitrum (or any other EVM chain where the same trusted signer is registered), call `PythLazer.verifyUpdate{value: 1 wei}(update)` with the identical bytes.
3. Both calls succeed and return the same `(payload, signer)` — the signature is accepted on the second chain despite never being produced for it.
4. A consuming protocol on Arbitrum that calls `verifyUpdate` and trusts the returned payload will accept the replayed price as current and chain-native.

Root cause: `hash = keccak256(payload)` at [5](#0-4)  contains no chain-binding context, making the signature portable across all EVM deployments of `PythLazer` that share the same trusted signer.

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L92-99)
```text
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
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
