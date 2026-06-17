### Title
Unauthenticated `/api/propose` Endpoint Allows Anyone to Submit Arbitrary Governance Proposals Using the Server's Multisig Signing Key - (File: `governance/xc_admin/packages/proposer_server/src/index.ts`)

---

### Summary

The `proposer_server` exposes a single HTTP endpoint `/api/propose` with no authentication, no authorization, and fully open CORS. Any external party who can reach the server can POST arbitrary Solana `TransactionInstruction` arrays, which the server will immediately package into a governance proposal and submit to the Pyth price-feed multisig using its own privileged `KEYPAIR`. The server also auto-approves every proposal it creates. This is a direct analog to H-07: anyone can inject proposals into the governance pipeline, bypassing the intended voting/review process.

---

### Finding Description

`governance/xc_admin/packages/proposer_server/src/index.ts` is a Node/Express HTTP server whose sole purpose is described in its own `package.json` as *"A server that proposes the instructions that it receives to the multisig."* [1](#0-0) 

The server applies `cors()` with no options (wildcard origin) and no authentication middleware before registering the only route: [2](#0-1) 

The handler deserializes arbitrary `TransactionInstruction` objects directly from the request body without any validation: [3](#0-2) 

It then constructs a `MultisigVault` backed by the server's own `KEYPAIR` (loaded from disk at startup) and calls `proposeInstructions`: [4](#0-3) 

`proposeInstructions` internally calls both `activateProposalIx` and `approveProposalIx` with the server's key, meaning the server's signing key **automatically approves every proposal it creates**: [5](#0-4) 

The legitimate frontend callers (`xc_admin_frontend`) use this server to submit price-feed configuration changes and publisher permissioning instructions: [6](#0-5) [7](#0-6) 

There is no IP allowlist, API key, JWT, HMAC, or any other access control in the entire server file.

---

### Impact Explanation

The server's `KEYPAIR` is a signer on `PRICE_FEED_MULTISIG`, the multisig that governs Pyth price-feed configuration across all supported chains. An attacker who can reach the server can:

1. **Submit malicious governance proposals** — e.g., add a malicious publisher to a price feed, remove legitimate publishers, or change price-feed parameters — using the server's key, which auto-approves the proposal.
2. **Spam the multisig** with hundreds of proposals, exhausting the transaction index, confusing legitimate signers, and degrading governance availability.
3. **Craft proposals that appear legitimate** to trick other multisig signers into approving them, since the proposal already carries the server's approval.

If the multisig threshold is 1 (or if the server's key carries sufficient weight), proposals execute immediately without any further human review. Even at higher thresholds, the attacker gains a persistent foothold in the governance queue.

---

### Likelihood Explanation

The server listens on a configurable `PORT` (default `4000`) with `app.use(cors())` accepting any origin. If the server is reachable from the public internet (as implied by its role serving the public `xc_admin_frontend`), any attacker can exploit this with a single `curl` command. No privileged access, leaked keys, or on-chain interaction is required — only HTTP reachability.

---

### Recommendation

Add authentication to the `/api/propose` endpoint before any other middleware. Options in increasing strength:

1. **Shared secret / API key**: Read a secret from an environment variable and require it as a `Bearer` token in the `Authorization` header. Reject all requests that do not match.
2. **IP allowlist**: Restrict the server to only accept connections from the known `xc_admin_frontend` deployment IP(s) or a private network.
3. **mTLS**: Require a client certificate signed by a Pyth-controlled CA.

At minimum, replace `app.use(cors())` with a strict origin allowlist and add a middleware that validates a shared secret:

```typescript
const API_SECRET = envOrErr("API_SECRET");
app.use((req, res, next) => {
  if (req.headers.authorization !== `Bearer ${API_SECRET}`) {
    return res.status(401).json("Unauthorized");
  }
  next();
});
```

---

### Proof of Concept

```bash
# Attacker submits a no-op instruction to verify unauthenticated access
curl -X POST http://<proposer_server_host>:4000/api/propose \
  -H "Content-Type: application/json" \
  -d '{
    "cluster": "mainnet-beta",
    "instructions": [{
      "programId": "11111111111111111111111111111111",
      "keys": [],
      "data": []
    }]
  }'
# Expected (vulnerable) response: {"proposalPubkey":"<base58_pubkey>"}
# The server's KEYPAIR has already signed and approved this proposal on the
# PRICE_FEED_MULTISIG. No authentication was required.
```

The attacker replaces the no-op with any instruction targeting the Pyth program (e.g., `addPublisher`, `delPublisher`, or cross-chain executor payloads) to inject malicious governance actions into the multisig queue.

### Citations

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L47-53)
```typescript
const app = express();

app.use(cors());
app.use(express.json({ limit: "50mb" }));
app.use(express.urlencoded({ extended: true, limit: "50mb" }));

app.post("/api/propose", async (req: Request, res: Response) => {
```

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L55-68)
```typescript
    const instructions: TransactionInstruction[] = req.body.instructions.map(
      (ix: any) =>
        new TransactionInstruction({
          data: Buffer.from(ix.data),
          keys: ix.keys.map((key: any) => {
            return {
              isSigner: key.isSigner,
              isWritable: key.isWritable,
              pubkey: new PublicKey(key.pubkey),
            };
          }),
          programId: new PublicKey(ix.programId),
        }),
    );
```

**File:** governance/xc_admin/packages/proposer_server/src/index.ts (L72-90)
```typescript
    const wallet = new NodeWallet(KEYPAIR);
    const proposeSquads: SquadsMesh = new SquadsMesh({
      connection: new Connection(RPC_URLS[getMultisigCluster(cluster)]),
      wallet,
    });

    const vault = new MultisigVault(
      wallet,
      getMultisigCluster(cluster),
      proposeSquads,
      PRICE_FEED_MULTISIG[getMultisigCluster(cluster)],
    );

    // preserve the existing API by returning only the first pubkey
    const proposalPubkey = (
      await vault.proposeInstructions(instructions, cluster, {
        computeUnitPriceMicroLamports: COMPUTE_UNIT_PRICE_MICROLAMPORTS!,
      })
    )[0];
```

**File:** governance/xc_admin/packages/xc_admin_common/src/propose.ts (L337-338)
```typescript
        ixToSend.push(await this.activateProposalIx(newProposalAddress));
        ixToSend.push(await this.approveProposalIx(newProposalAddress));
```

**File:** governance/xc_admin/packages/xc_admin_frontend/components/programs/PythCore.tsx (L493-496)
```typescript
        const response = await axios.post(proposerServerUrl + "/api/propose", {
          cluster,
          instructions,
        });
```

**File:** governance/xc_admin/packages/xc_admin_frontend/components/PermissionDepermissionKey.tsx (L137-140)
```typescript
        const response = await axios.post(proposerServerUrl + "/api/propose", {
          cluster,
          instructions,
        });
```
