# REJKT Platform — Master Roadmap & Prompt Reference

> **What this is:** A self-hostable NFT storefront + deployer platform on Tezos. Creators deploy their own FA2 token contracts with custom symbols, get a branded storefront, and list tokens on objkt.com. The platform is free to use, with optional community support.

---

## Core Philosophy

- **Free to deploy.** No platform fee. Gas/storage costs (~1–3 tez) paid by the deployer.
- **Fee is optional and on-chain.** Marketplace contracts include a `fee_percent` param (default 0). The deployer sets it themselves. You cannot force a fee on self-hosters.
- **Open source everything.** SmartPy contracts, frontend, deployer UI — all public on GitHub.
- **Self-hostable or hosted.** Creators can run it on their own VPS or use a hosted version you maintain.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Smart contracts | SmartPy (compiled to Michelson) |
| Tezos interaction | Taquito + Beacon Wallet |
| Frontend | Next.js (React) |
| Styling | Tailwind CSS |
| IPFS pinning | Pinata API (configurable) |
| Blockchain indexing | TzKT public API (free, no self-hosting) |
| VPS hosting | DreamHost VPS / Nginx / PM2 |
| Testnet | Tezos Ghostnet |

---

## Project Structure

```
rejkt-platform/
├── contracts/
│   ├── fa2_token.py           # FA2 token contract (SmartPy)
│   ├── marketplace.py         # Swap/collect contract with fee param
│   └── tests/                 # SmartPy unit tests
├── frontend/
│   ├── storefront/            # Per-creator NFT storefront (Next.js)
│   │   ├── config.js          # THE file creators edit to brand their store
│   │   └── ...
│   └── deployer/              # Deployer UI (Next.js)
│       └── ...
├── scripts/
│   ├── deploy_testnet.sh      # Ghostnet deployment helper
│   └── deploy_mainnet.sh      # Mainnet deployment helper
├── docs/
│   └── self-hosting.md        # VPS setup guide (Nginx, PM2, Node)
└── README.md
```

---

## config.js — The Creator's Single Control File

```javascript
export default {
  // Branding
  storeName: "REJKT",
  storeDescription: "Your collection description here",
  creatorAddress: "tz1YOUR_WALLET",

  // Contracts
  fa2ContractAddress: "KT1YOUR_FA2",
  marketplaceContractAddress: "KT1YOUR_MARKETPLACE",

  // Fee settings (set at marketplace contract deploy time — cannot change after)
  feePercent: 0,               // 0 = free. Max recommended: 2.5
  feeRecipient: "tz1YOUR_WALLET",

  // Existing objkt collection (optional — skip FA2 deploy, index existing tokens)
  indexFromObjkt: false,
  objktContractAddress: "",    // Fill if indexFromObjkt = true

  // IPFS
  pinataApiKey: "",
  pinataSecretKey: "",

  // Tezos RPC node
  rpcNode: "https://mainnet.api.tez.ie",

  // Theme
  theme: {
    primaryColor: "#000000",
    backgroundColor: "#ffffff",
    fontFamily: "serif"
  }
}
```

---

## Phases

### Phase 1 — Contracts (Testnet)
**Goal:** Working FA2 + marketplace contracts deployed on Ghostnet.

- [ ] Write FA2 token contract in SmartPy
  - Configurable symbol, name, admin address
  - `mint` entry point (admin only)
  - TZIP-12 compliant (standard FA2)
  - TZIP-16 contract metadata (for objkt collection display)
- [ ] Write marketplace contract in SmartPy
  - `swap` entry point (list token for sale)
  - `collect` entry point (buy token)
  - `cancel_swap` entry point
  - `fee_percent` storage param (set at origination, immutable)
  - `fee_recipient` storage param (set at origination)
  - Royalty pass-through to original creator
- [ ] Write SmartPy unit tests for both contracts
- [ ] Deploy both to Ghostnet
- [ ] Verify on better-call.dev (Ghostnet)

**Deliverable:** Two `.py` contract files + Ghostnet contract addresses.

---

### Phase 2 — Storefront Frontend (Testnet)
**Goal:** Working storefront that reads from your Ghostnet contracts via TzKT.

- [ ] Next.js project scaffold with Tailwind
- [ ] `config.js` wired throughout (no hardcoded values anywhere else)
- [ ] Wallet connection (Beacon Wallet)
- [ ] Token gallery — reads minted tokens from TzKT API
- [ ] Token detail page — metadata, editions, price
- [ ] Mint page (admin only — checks wallet = creatorAddress)
  - Upload file to IPFS via Pinata
  - Upload metadata JSON to IPFS (TZIP-21 format)
  - Call `mint` on FA2 contract via Taquito
- [ ] Swap/list page — set price, call marketplace `swap`
- [ ] Collect/buy flow — call marketplace `collect`
- [ ] Cancel listing — call `cancel_swap`
- [ ] objkt passthrough mode — if `indexFromObjkt: true`, skip FA2 calls, read tokens from TzKT by contract address

**Deliverable:** Runnable Next.js storefront pointed at Ghostnet.

---

### Phase 3 — Deployer UI (Testnet)
**Goal:** A web UI where anyone can deploy their own FA2 + marketplace to Ghostnet.

- [ ] Deployer form:
  - Token name, symbol, description
  - Creator wallet address
  - Fee percent (0–2.5%, slider)
  - Fee recipient address
  - IPFS Pinata keys
- [ ] Compile + originate FA2 contract via Taquito (using pre-compiled Michelson)
- [ ] Compile + originate marketplace contract via Taquito
- [ ] Generate a ready-to-use `config.js` for the deployer to download
- [ ] Show deployer their new contract addresses and next steps
- [ ] Link to self-hosting docs

**Note:** Pre-compiled Michelson is bundled with the deployer. No server-side compilation needed.

**Deliverable:** Deployer UI running on Ghostnet.

---

### Phase 4 — DreamHost VPS Setup
**Goal:** All three components (storefront, deployer, contracts) running on mainnet on DreamHost.

- [ ] DreamHost VPS: Install Node 18+, Nginx, PM2, Certbot (SSL)
- [ ] Nginx config: route domains/subdomains to Next.js processes
  - `yourdomain.com` → storefront
  - `deploy.yourdomain.com` → deployer UI
- [ ] PM2 config: keep both Next.js apps alive, auto-restart
- [ ] SSL certificates via Let's Encrypt (Certbot)
- [ ] Environment variables for Pinata keys (not in config.js for security)
- [ ] Deploy FA2 + marketplace to **Tezos mainnet**
- [ ] Register collection on objkt.com (wallet → Manage Collections)
- [ ] Mint first REJKT token as live test

**Deliverable:** Live mainnet storefront + deployer on your domain.

---

### Phase 5 — Polish & Open Source Release
**Goal:** Clean GitHub repo others can actually use.

- [ ] `README.md` — what it is, why it exists, how to self-host
- [ ] `docs/self-hosting.md` — full VPS setup walkthrough (DreamHost or any Ubuntu VPS)
- [ ] `docs/deploy-your-own.md` — how to use the deployer UI vs SmartPy IDE
- [ ] `docs/objkt-integration.md` — how to link an existing objkt collection
- [ ] License: MIT
- [ ] GitHub repo: `rejkt-platform` (or your chosen name)
- [ ] Optional: submit to Teia community board / Tezos developer Discord for visibility

---

## Key API References

| Service | Use | URL |
|---|---|---|
| TzKT | Index tokens, swaps, wallets | `https://api.tzkt.io/v1/` |
| SmartPy IDE | Write/test/compile contracts | `https://smartpy.io/ide` |
| better-call.dev | Inspect deployed contracts | `https://better-call.dev` |
| Pinata | IPFS pinning | `https://pinata.cloud` |
| Ghostnet faucet | Free testnet tez | `https://faucet.ghostnet.teztnets.com` |
| Beacon Wallet docs | Wallet integration | `https://docs.walletbeacon.io` |
| Taquito docs | Tezos JS library | `https://tezostaquito.io/docs` |
| objkt collection mgmt | Register your contract | `https://objkt.com/manage/collections` |

---

## Important Contract Notes

- **FA2 contracts on Tezos are not upgradeable.** Test thoroughly on Ghostnet before mainnet.
- **Fee percent is set at origination** and cannot be changed. If you want to change it later, you deploy a new marketplace contract.
- **The FA2 token contract and marketplace contract are separate.** The FA2 holds tokens; the marketplace handles sales escrow.
- **Royalties** are stored in FA2 token metadata at mint time. Marketplace contract reads and enforces them on collect.
- **Token IDs** in your FA2 start at 0 and increment — completely independent from OBJKTs.

---

## Building Session Protocol

When resuming work with Claude, start with:

> "I'm building the REJKT platform. Here is the roadmap: [paste this file]. We are currently on Phase [X], working on [specific task]. Here is what we have so far: [paste relevant files or contract addresses]."

This file is the source of truth. Update the checkboxes as phases complete.

---

## Current Status

- [x] Architecture decided
- [x] Roadmap written
- [ ] Phase 1 — Contracts (next)
- [ ] Phase 2 — Storefront
- [ ] Phase 3 — Deployer UI
- [ ] Phase 4 — Mainnet + DreamHost
- [ ] Phase 5 — Open Source Release

---

*Last updated: April 2026*
