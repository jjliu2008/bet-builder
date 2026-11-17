"""Generate a Solana devnet keypair for onchain receipts and try to fund it.

Saves the keypair (solana-keygen JSON byte-array format) OUTSIDE the repo, at
~/.config/bet-builder/devnet.json, prints the public key and the base58 secret to paste into the
host's SOLANA_KEYPAIR env var, then requests a 1 SOL devnet airdrop.

Usage:  python scripts/new_keypair.py [--out PATH] [--force] [--no-airdrop]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests
from solders.keypair import Keypair

DEFAULT_OUT = os.path.join(os.path.expanduser("~"), ".config", "bet-builder", "devnet.json")
RPC = os.environ.get("SOLANA_RPC_URL") or "https://api.devnet.solana.com"


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(body["error"].get("message", body["error"]))
    return body["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--force", action="store_true", help="overwrite an existing keypair file")
    ap.add_argument("--no-airdrop", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        with open(args.out, encoding="utf-8") as f:
            kp = Keypair.from_bytes(bytes(json.load(f)))
        print(f"Keypair already exists at {args.out} (use --force to replace); reusing it.")
    else:
        kp = Keypair()
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(list(bytes(kp)), f)
        try:
            os.chmod(args.out, 0o600)
        except OSError:
            pass
        print(f"Saved new devnet keypair to {args.out}")

    pub = str(kp.pubkey())
    print(f"\nPublic key:      {pub}")
    print(f"Base58 secret:   {kp}")
    print("  -> on the deployed backend set SOLANA_KEYPAIR to the base58 secret above.")
    print(f"  -> locally you can instead set SOLANA_KEYPAIR_PATH={args.out}")
    print("  (devnet only: never reuse this key for real funds, never commit it)\n")

    if args.no_airdrop:
        return
    try:
        sig = rpc("requestAirdrop", [pub, 1_000_000_000])
        print(f"Airdrop requested: {sig}")
        for _ in range(30):
            time.sleep(1)
            bal = rpc("getBalance", [pub, {"commitment": "confirmed"}])["value"]
            if bal > 0:
                print(f"Balance: {bal / 1e9:.4f} SOL (devnet). Ready.")
                return
        print("Airdrop not confirmed yet; check the balance again in a minute.")
    except Exception as e:  # faucet rate limits are common
        print(f"Airdrop failed ({e}).")
        print(f"Fund it manually: open https://faucet.solana.com , choose devnet, paste {pub}")
        sys.exit(2)


if __name__ == "__main__":
    main()
