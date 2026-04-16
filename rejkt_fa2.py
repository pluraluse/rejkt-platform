"""
REJKT FA2 Token Contract
========================
A multi-edition NFT contract for the REJKT collection on Tezos.

Features:
  - Admin-only drop creation (open or limited editions)
  - Anyone can collect/mint by paying the drop price
  - Per-contract royalty (set at origination, applies to all tokens)
  - Royalty recipient stored per-token for future splits upgrade path
  - TZIP-12 (FA2) and TZIP-16 (contract metadata) compliant
  - Token IDs are sequential starting at 0, unique to this contract

Instructions:
  1. Paste this file into the SmartPy IDE at https://smartpy.io/ide
  2. Replace ADMIN_ADDRESS with your Tezos wallet address (tz1...)
  3. Replace ROYALTY_PERCENT with your desired royalty (e.g. 150 = 15%)
  4. Replace CONTRACT_METADATA_URI with your IPFS metadata URI after uploading
  5. Run tests, then deploy to Ghostnet first, then Mainnet

Upgrade path notes:
  - royalty_recipients is stored as a map per token to support future splits
  - Currently always set to {admin: royalty_percent} at mint time
  - v2 will allow multiple recipients with split percentages
"""

import smartpy as sp
from smartpy.templates import fa2_lib as fa2

# ── Constants (edit these before deploying) ───────────────────────────────────

ADMIN_ADDRESS       = "tz1YourWalletAddressHere"   # Your Tezos wallet
ROYALTY_PERCENT     = 150                           # 150 = 15% (out of 1000)
CONTRACT_METADATA_URI = "ipfs://YourContractMetadataCIDHere"

# ─────────────────────────────────────────────────────────────────────────────

main = fa2.main


@sp.module
def rejkt_module():
    import main

    # ── Storage types ─────────────────────────────────────────────────────────

    # A drop is what the admin creates. Collectors mint from it.
    drop_type: type = sp.record(
        price        = sp.mutez,        # price per edition in mutez (0 = free)
        max_editions = sp.option[sp.nat],  # None = open edition
        minted       = sp.nat,          # how many have been collected so far
        active       = sp.bool,         # admin can pause/close a drop
        token_id     = sp.nat,          # the FA2 token ID for this drop
    )

    # Royalty recipient map — ready for v2 splits (currently single entry)
    royalty_map_type: type = sp.map[sp.address, sp.nat]

    # ── Contract ──────────────────────────────────────────────────────────────

    class REJKTToken(
        main.Admin,
        main.MintFungible,
        main.Fungible,
        main.OnchainviewBalanceOf,
    ):
        def __init__(self, admin_address, contract_metadata, royalty_percent):
            # Init FA2 base with empty ledger and token metadata
            main.OnchainviewBalanceOf.__init__(self)
            main.MintFungible.__init__(self)
            main.Fungible.__init__(self, contract_metadata, {}, [])
            main.Admin.__init__(self, admin_address)

            # REJKT-specific storage
            self.data.royalty_percent = sp.cast(royalty_percent, sp.nat)
            self.data.next_drop_id    = sp.nat(0)
            self.data.next_token_id   = sp.nat(0)
            self.data.drops           = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, drop_type]
            )
            # Per-token royalty recipients — supports future splits
            self.data.royalty_recipients = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, royalty_map_type]
            )

        # ── Admin: Create a drop ─────────────────────────────────────────────

        @sp.entrypoint
        def create_drop(self, params):
            """
            Admin creates a new drop (token type).
            params:
              - metadata     : sp.map[sp.string, sp.bytes]  (TZIP-21 token metadata)
              - price        : sp.mutez                      (0 = free)
              - max_editions : sp.option[sp.nat]             (None = open edition)
            """
            sp.verify(sp.sender == self.data.administrator, "NOT_ADMIN")

            token_id = self.data.next_token_id

            # Register token metadata in FA2 big_map
            self.data.token_metadata[token_id] = sp.record(
                token_id   = token_id,
                token_info = params.metadata,
            )

            # Store royalty recipients for this token (single for v1)
            self.data.royalty_recipients[token_id] = {
                self.data.administrator: self.data.royalty_percent
            }

            # Register the drop
            self.data.drops[self.data.next_drop_id] = sp.record(
                price        = params.price,
                max_editions = params.max_editions,
                minted       = sp.nat(0),
                active       = True,
                token_id     = token_id,
            )

            self.data.next_drop_id  += 1
            self.data.next_token_id += 1

        # ── Admin: Open / close a drop ───────────────────────────────────────

        @sp.entrypoint
        def set_drop_active(self, params):
            """
            Admin opens or closes a drop.
            params:
              - drop_id : sp.nat
              - active  : sp.bool
            """
            sp.verify(sp.sender == self.data.administrator, "NOT_ADMIN")
            sp.verify(self.data.drops.contains(params.drop_id), "UNKNOWN_DROP")
            self.data.drops[params.drop_id].active = params.active

        # ── Public: Collect (mint) from a drop ───────────────────────────────

        @sp.entrypoint
        def collect(self, params):
            """
            Anyone can collect an edition from an active drop by paying the price.
            params:
              - drop_id  : sp.nat
              - quantity : sp.nat   (how many editions to collect, min 1)
            """
            sp.verify(self.data.drops.contains(params.drop_id), "UNKNOWN_DROP")

            drop = self.data.drops[params.drop_id]

            sp.verify(drop.active, "DROP_NOT_ACTIVE")
            sp.verify(params.quantity >= sp.nat(1), "MIN_ONE")

            # Check edition limit if not open edition
            sp.if drop.max_editions.is_some():
                sp.verify(
                    drop.minted + params.quantity <= drop.max_editions.unwrap_some(),
                    "SOLD_OUT"
                )

            # Check payment
            total_price = sp.split_tokens(drop.price, params.quantity, sp.nat(1))
            sp.verify(sp.amount == total_price, "WRONG_AMOUNT")

            token_id = drop.token_id

            # Mint editions to collector
            sp.if self.data.ledger.contains((sp.sender, token_id)):
                self.data.ledger[(sp.sender, token_id)] += params.quantity
            sp.else:
                self.data.ledger[(sp.sender, token_id)] = params.quantity

            # Update total supply in token_metadata (informational)
            self.data.drops[params.drop_id].minted += params.quantity

            # Send payment to admin
            sp.if sp.amount > sp.mutez(0):
                sp.send(self.data.administrator, sp.amount)

        # ── Admin: Update royalty percent (v1: global, v2: per-token) ────────

        @sp.entrypoint
        def update_royalty(self, new_royalty_percent):
            """
            Admin updates the global royalty percent.
            Does NOT retroactively change already-minted tokens.
            New drops will use the updated value.
            new_royalty_percent: sp.nat (e.g. 150 = 15%, max 250 = 25%)
            """
            sp.verify(sp.sender == self.data.administrator, "NOT_ADMIN")
            sp.verify(new_royalty_percent <= sp.nat(250), "MAX_ROYALTY_25_PERCENT")
            self.data.royalty_percent = new_royalty_percent

        # ── View: Get royalty info for a token (for marketplace contract) ─────

        @sp.onchain_view()
        def get_royalties(self, token_id):
            """
            Returns the royalty map for a given token_id.
            Used by the marketplace contract to distribute royalties on sale.
            """
            sp.verify(self.data.royalty_recipients.contains(token_id), "UNKNOWN_TOKEN")
            return self.data.royalty_recipients[token_id]

        # ── View: Get drop info ───────────────────────────────────────────────

        @sp.onchain_view()
        def get_drop(self, drop_id):
            """Returns full drop record for a given drop_id."""
            sp.verify(self.data.drops.contains(drop_id), "UNKNOWN_DROP")
            return self.data.drops[drop_id]


# ── Tests ─────────────────────────────────────────────────────────────────────

@sp.add_test()
def test():
    scenario = sp.test_scenario("REJKT FA2 Tests", rejkt_module)
    scenario.h1("REJKT Token Contract Tests")

    # Test accounts
    admin   = sp.test_account("Admin")
    alice   = sp.test_account("Alice")
    bob     = sp.test_account("Bob")

    # Helper: build minimal TZIP-21 token metadata
    def make_token_metadata(name, symbol, ipfs_cid):
        def b(s): return sp.bytes("0x" + s.encode("utf-8").hex())
        return {
            "name"        : b(name),
            "symbol"      : b(symbol),
            "decimals"    : b("0"),
            "artifactUri" : b("ipfs://" + ipfs_cid),
            "displayUri"  : b("ipfs://" + ipfs_cid),
            "thumbnailUri": b("ipfs://" + ipfs_cid),
            "description" : b("A REJKT token."),
            "minter"      : b(admin.address.__str__()),
        }

    # Deploy contract
    scenario.h2("1. Deploy")
    contract = rejkt_module.REJKTToken(
        admin_address     = admin.address,
        contract_metadata = sp.big_map({"": sp.bytes("0x" + CONTRACT_METADATA_URI.encode("utf-8").hex())}),
        royalty_percent   = ROYALTY_PERCENT,
    )
    scenario += contract
    scenario.verify(contract.data.royalty_percent == ROYALTY_PERCENT)

    # ── Test: Create a limited drop ───────────────────────────────────────────
    scenario.h2("2. Admin creates a limited drop (10 editions, 1 tez each)")
    contract.create_drop(
        sp.record(
            metadata     = make_token_metadata("REJKT #0", "REJKT", "QmFakeCID0"),
            price        = sp.tez(1),
            max_editions = sp.some(sp.nat(10)),
        ),
        _sender = admin
    )
    scenario.verify(contract.data.next_drop_id == sp.nat(1))
    scenario.verify(contract.data.next_token_id == sp.nat(1))

    # ── Test: Create an open edition drop ────────────────────────────────────
    scenario.h2("3. Admin creates an open edition drop (free)")
    contract.create_drop(
        sp.record(
            metadata     = make_token_metadata("REJKT #1", "REJKT", "QmFakeCID1"),
            price        = sp.mutez(0),
            max_editions = sp.none,
        ),
        _sender = admin
    )
    scenario.verify(contract.data.next_drop_id == sp.nat(2))

    # ── Test: Collect from limited drop ──────────────────────────────────────
    scenario.h2("4. Alice collects 2 editions from drop 0")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(2)),
        _sender = alice,
        _amount = sp.tez(2)
    )
    scenario.verify(contract.data.ledger[(alice.address, sp.nat(0))] == sp.nat(2))
    scenario.verify(contract.data.drops[sp.nat(0)].minted == sp.nat(2))

    # ── Test: Collect from open edition ──────────────────────────────────────
    scenario.h2("5. Bob collects 1 free edition from drop 1")
    contract.collect(
        sp.record(drop_id=sp.nat(1), quantity=sp.nat(1)),
        _sender = bob,
        _amount = sp.mutez(0)
    )
    scenario.verify(contract.data.ledger[(bob.address, sp.nat(1))] == sp.nat(1))

    # ── Test: Wrong payment rejected ─────────────────────────────────────────
    scenario.h2("6. Alice tries to underpay — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(1)),
        _sender = alice,
        _amount = sp.mutez(500000),   # 0.5 tez instead of 1
        _valid  = False               # expect failure
    )

    # ── Test: Non-admin cannot create drop ───────────────────────────────────
    scenario.h2("7. Alice tries to create a drop — should fail")
    contract.create_drop(
        sp.record(
            metadata     = make_token_metadata("FAKE", "FAKE", "QmFake"),
            price        = sp.tez(1),
            max_editions = sp.some(sp.nat(5)),
        ),
        _sender = alice,
        _valid  = False
    )

    # ── Test: Close drop ─────────────────────────────────────────────────────
    scenario.h2("8. Admin closes drop 1")
    contract.set_drop_active(
        sp.record(drop_id=sp.nat(1), active=False),
        _sender = admin
    )
    scenario.verify(~contract.data.drops[sp.nat(1)].active)

    # ── Test: Collect from closed drop fails ─────────────────────────────────
    scenario.h2("9. Bob tries to collect from closed drop — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(1), quantity=sp.nat(1)),
        _sender = bob,
        _amount = sp.mutez(0),
        _valid  = False
    )

    # ── Test: Sold out ────────────────────────────────────────────────────────
    scenario.h2("10. Alice tries to over-collect limited drop — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(9)),  # 2 already minted, only 8 left
        _sender = alice,
        _amount = sp.tez(9),
        _valid  = False
    )

    # ── Test: Update royalty ──────────────────────────────────────────────────
    scenario.h2("11. Admin updates royalty to 10%")
    contract.update_royalty(sp.nat(100), _sender=admin)
    scenario.verify(contract.data.royalty_percent == sp.nat(100))

    # ── Test: Royalty view ────────────────────────────────────────────────────
    scenario.h2("12. Check royalty recipients for token 0")
    scenario.verify(
        contract.get_royalties(sp.nat(0))[admin.address] == sp.nat(ROYALTY_PERCENT)
    )

    scenario.h1("All tests passed.")


# ── Compilation target ────────────────────────────────────────────────────────
# This is what SmartPy compiles to Michelson for deployment.

sp.add_compilation_target(
    "REJKT_FA2",
    rejkt_module.REJKTToken(
        admin_address     = sp.address(ADMIN_ADDRESS),
        contract_metadata = sp.big_map({
            "": sp.bytes("0x" + CONTRACT_METADATA_URI.encode("utf-8").hex())
        }),
        royalty_percent   = sp.nat(ROYALTY_PERCENT),
    )
)
