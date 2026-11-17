"""An in-memory stand-in for the Solana JSON-RPC: accepts real signed transactions, records memos."""
from solders.hash import Hash
from solders.transaction import Transaction

import chain


class FakeLedger:
    def __init__(self):
        self.txs = {}            # signature -> memo
        self.calls = []
        self.blockhash = str(Hash.new_unique())

    def __call__(self, method, params, timeout):
        self.calls.append(method)
        assert timeout <= chain.TIMEOUT_S + 1e-9
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": self.blockhash, "lastValidBlockHeight": 100}}
        if method == "sendTransaction":
            import base64
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            tx.verify()                                           # the signature must be valid
            ix = tx.message.instructions[0]
            program = tx.message.account_keys[ix.program_id_index]
            assert str(program) == chain.MEMO_PROGRAM_ID
            sig = str(tx.signatures[0])
            self.txs[sig] = bytes(ix.data).decode("utf-8")
            return sig
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None} if s in self.txs else None
                              for s in params[0]]}
        if method == "getTransaction":
            memo = self.txs.get(params[0])
            if memo is None:
                return None
            return {"meta": {"err": None, "logMessages": []},
                    "transaction": {"message": {"instructions": [
                        {"program": "spl-memo", "programId": chain.MEMO_PROGRAM_ID, "parsed": memo}]}}}
        raise AssertionError(f"unexpected RPC {method}")
