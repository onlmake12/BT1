### Title
Unauthenticated `owner` Parameter in `create_stake_account` Allows Front-Running to Hijack Stake Account Ownership - (File: `governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The `create_stake_account` instruction in the Pyth staking program is explicitly documented as "trustless" and accepts an arbitrary `owner` pubkey as an instruction argument with no verification that the caller controls the `stakeAccountPositions` account. An attacker who observes a freshly-created but uninitialized `stakeAccountPositions` account can front-run the victim's `create_stake_account` call and set themselves as the `owner`, gaining full control over the stake account and any PYTH tokens subsequently deposited to its custody vault.

---

### Finding Description

The `create_stake_account` instruction has the following account structure:

- `payer` — signer, writable (pays rent)
- `stakeAccountPositions` — **writable, NOT a signer, no ownership constraint**
- `stakeAccountMetadata` — PDA derived from `stakeAccountPositions`
- `stakeAccountCustody` — PDA derived from `stakeAccountPositions`

The instruction arguments include `owner: pubkey` and `lock: VestingSchedule`. Neither the `owner` argument nor the `stakeAccountPositions` account is authenticated against the `payer`. Any caller can invoke `create_stake_account` with any `stakeAccountPositions` address and any `owner` pubkey.

The design comment in the IDL explicitly states: *"The main account i.e. the position accounts needs to be initialized outside of the program, otherwise we run into stack limits."* This means `stakeAccountPositions` is created in a prior `SystemProgram.createAccountWithSeed` call, creating a window — however brief — between account creation and metadata initialization.

The `StakeAccountMetadataV2` struct stores the `owner` field, and all privileged operations (`create_position`, `close_position`, `withdraw_stake`, `join_dao_llc`, etc.) gate access via `relations: ["stake_account_metadata"]` on the `owner` signer. Whoever is recorded as `owner` in the metadata controls the account entirely. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

An attacker who front-runs `create_stake_account` with `owner = attacker_pubkey` causes:

1. `stakeAccountMetadata.owner` is set to the attacker's key.
2. `stakeAccountPositions.data.owner` is set to the attacker's key.
3. The victim's subsequent `create_stake_account` call fails (account already initialized).
4. The victim, unaware of the hijack, deposits PYTH tokens to `stakeAccountCustody` (a PDA derived from `stakeAccountPositions`, not from the owner).
5. The attacker, as the recorded `owner`, can call `create_position`, `close_position`, and `withdraw_stake` to drain the custody vault.

This results in **direct theft of staked PYTH tokens** from the victim. [3](#0-2) 

---

### Likelihood Explanation

The SDK (`pyth-staking-client.ts`) bundles `createAccountWithSeed` and `create_stake_account` in the same transaction, making the window atomic for SDK users. However:

1. **Non-SDK callers**: Any user interacting with the program directly (e.g., via CLI, custom scripts, or integrations) may issue the two steps in separate transactions, creating an exploitable window.
2. **Transaction retry after failure**: If the bundled transaction fails after `createAccountWithSeed` succeeds (e.g., due to a race on the positions account), the retry exposes the uninitialized account.
3. **The program itself has zero protection**: The on-chain program enforces no constraint linking `payer` to `stakeAccountPositions`. The SDK's atomic bundling is a client-side convention, not a protocol guarantee.
4. **Mempool monitoring is feasible on Solana**: Validators and MEV bots routinely observe pending transactions. [4](#0-3) 

---

### Recommendation

**Short Term**: Add a constraint in the `CreateStakeAccount` Anchor accounts struct requiring that `stakeAccountPositions` is owned by the staking program **and** that its data `owner` field matches the `payer` signer, or alternatively require `stakeAccountPositions` to be a signer (which is possible if the account is created via a CPI within the same instruction using a keypair).

**Long Term**: Redesign the instruction so that `stakeAccountPositions` is initialized atomically within the program using a PDA derived from the `payer`'s key (e.g., `seeds = [b"positions", payer.key()]`), eliminating the external pre-initialization requirement entirely and removing the front-running surface.

---

### Proof of Concept

```
1. Alice submits Tx A: SystemProgram.createAccountWithSeed {
     basePubkey: alice,
     seed: "abc123",
     programId: staking_program,
     newAccountPubkey: positions_addr   // deterministic, observable
   }

2. Eve observes Tx A in the mempool, extracts positions_addr.

3. Eve submits Tx B (higher priority fee):
     create_stake_account(
       payer: eve,
       stakeAccountPositions: positions_addr,   // Alice's account
       owner: eve_pubkey,                        // Eve's key
       lock: { fullyVested: {} }
     )
   → stakeAccountMetadata.owner = eve_pubkey ✓

4. Alice's Tx C: create_stake_account(..., owner: alice_pubkey) → FAILS
   (stakeAccountMetadata already initialized)

5. Alice, confused, deposits 10,000 PYTH to stakeAccountCustody
   (custody PDA is derived from positions_addr, not from owner).

6. Eve calls create_position / close_position / withdraw_stake
   as the recorded owner → drains Alice's 10,000 PYTH.
``` [5](#0-4) [6](#0-5)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L661-772)
```json
      "accounts": [
        {
          "name": "payer",
          "signer": true,
          "writable": true
        },
        {
          "name": "stake_account_positions",
          "writable": true
        },
        {
          "name": "stake_account_metadata",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "name": "stake_account_custody",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "custody_authority",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [97, 117, 116, 104, 111, 114, 105, 116, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "pyth_token_mint",
          "relations": ["config"]
        },
        {
          "address": "SysvarRent111111111111111111111111111111111",
          "name": "rent"
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "owner",
          "type": "pubkey"
        },
        {
          "name": "lock",
          "type": {
            "defined": {
              "name": "VestingSchedule"
            }
          }
        }
      ],
      "discriminator": [105, 24, 131, 19, 201, 250, 157, 73],
      "docs": [
        "Trustless instruction that creates a stake account for a user",
        "The main account i.e. the position accounts needs to be initialized outside of the program",
        "otherwise we run into stack limits"
      ],
      "name": "create_stake_account"
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L2011-2065)
```json
        "It is derived from the positions account with seeds \"stake_metadata\" and the positions account",
        "pubkey It stores some PDA bumps, the owner of the account and the vesting schedule"
      ],
      "name": "StakeAccountMetadataV2",
      "type": {
        "fields": [
          {
            "name": "metadata_bump",
            "type": "u8"
          },
          {
            "name": "custody_bump",
            "type": "u8"
          },
          {
            "name": "authority_bump",
            "type": "u8"
          },
          {
            "name": "voter_bump",
            "type": "u8"
          },
          {
            "name": "owner",
            "type": "pubkey"
          },
          {
            "name": "lock",
            "type": {
              "defined": {
                "name": "VestingSchedule"
              }
            }
          },
          {
            "name": "next_index",
            "type": "u8"
          },
          {
            "name": "_deprecated",
            "type": {
              "option": "u64"
            }
          },
          {
            "name": "signed_agreement_hash",
            "type": {
              "option": {
                "array": ["u8", 32]
              }
            }
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L492-614)
```typescript
      name: "createStakeAccount";
      docs: [
        "Trustless instruction that creates a stake account for a user",
        "The main account i.e. the position accounts needs to be initialized outside of the program",
        "otherwise we run into stack limits",
      ];
      discriminator: [105, 24, 131, 19, 201, 250, 157, 73];
      accounts: [
        {
          name: "payer";
          writable: true;
          signer: true;
        },
        {
          name: "stakeAccountPositions";
          writable: true;
        },
        {
          name: "stakeAccountMetadata";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  115,
                  116,
                  97,
                  107,
                  101,
                  95,
                  109,
                  101,
                  116,
                  97,
                  100,
                  97,
                  116,
                  97,
                ];
              },
              {
                kind: "account";
                path: "stakeAccountPositions";
              },
            ];
          };
        },
        {
          name: "stakeAccountCustody";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [99, 117, 115, 116, 111, 100, 121];
              },
              {
                kind: "account";
                path: "stakeAccountPositions";
              },
            ];
          };
        },
        {
          name: "custodyAuthority";
          docs: ["CHECK : This AccountInfo is safe because it's a checked PDA"];
          pda: {
            seeds: [
              {
                kind: "const";
                value: [97, 117, 116, 104, 111, 114, 105, 116, 121];
              },
              {
                kind: "account";
                path: "stakeAccountPositions";
              },
            ];
          };
        },
        {
          name: "config";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "pythTokenMint";
          relations: ["config"];
        },
        {
          name: "rent";
          address: "SysvarRent111111111111111111111111111111111";
        },
        {
          name: "tokenProgram";
          address: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
      ];
      args: [
        {
          name: "owner";
          type: "pubkey";
        },
        {
          name: "lock";
          type: {
            defined: {
              name: "vestingSchedule";
            };
          };
        },
      ];
    },
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L467-496)
```typescript
    const nonce = crypto.randomBytes(16).toString("hex");
    const stakeAccountPositions = await PublicKey.createWithSeed(
      this.wallet.publicKey,
      nonce,
      this.stakingProgram.programId,
    );

    const minimumBalance =
      await this.stakingProgram.provider.connection.getMinimumBalanceForRentExemption(
        POSITIONS_ACCOUNT_SIZE,
      );

    const instructions = [];

    instructions.push(
      SystemProgram.createAccountWithSeed({
        basePubkey: this.wallet.publicKey,
        fromPubkey: this.wallet.publicKey,
        lamports: minimumBalance,
        newAccountPubkey: stakeAccountPositions,
        programId: this.stakingProgram.programId,
        seed: nonce,
        space: POSITIONS_ACCOUNT_SIZE,
      }),
      await this.stakingProgram.methods
        .createStakeAccount(this.wallet.publicKey, { fullyVested: {} })
        .accounts({
          stakeAccountPositions,
        })
        .instruction(),
```
