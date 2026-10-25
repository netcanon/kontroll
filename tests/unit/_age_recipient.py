"""Age recipients for the tests, built rather than written out.

kontroll-init takes a break-glass recipient only as an X25519 one: lower-case bech32 under the human part `age` with
a valid BIP-173 checksum, exactly 32 bytes, and not a low-order point (it refuses a plugin or post-quantum
recipient, which no kontroll image can encrypt to). A test that hands one in needs a value that passes; a
real-length key written into the source is what the leak guard looks for, so each is built here, at import, from
fixed bytes.

Leading-underscore module name ⇒ pytest does not collect it as a test; it is imported by the `test_*` files.
"""

_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GEN = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)


def bech32(hrp, data):
    """`data` (bytes) as lower-case bech32 under the human part `hrp`, with a valid BIP-173 checksum."""
    bits, acc, nbits = [], 0, 0
    for byte in data:
        acc, nbits = (acc << 8) | byte, nbits + 8
        while nbits >= 5:
            nbits -= 5
            bits.append((acc >> nbits) & 31)
    if nbits:
        bits.append((acc << (5 - nbits)) & 31)
    chk = 1
    for v in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + bits + [0] * 6:
        b, chk = chk >> 25, ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= _GEN[i] if (b >> i) & 1 else 0
    return hrp + "1" + "".join(_ALPHABET[d] for d in bits + [((chk ^ 1) >> 5 * (5 - i)) & 31 for i in range(6)])


def x25519_recipient(start=0, ending=""):
    """An X25519 age recipient as age-keygen prints one (`age1` and 58 characters): the first, from byte `start` on,
    of 32 copies of one byte whose encoding ends in `ending`. Byte 0 is skipped: 32 zero bytes are the low-order
    point age refuses at encrypt (no other byte spells a point of small order)."""
    return next(k for k in (bech32("age", bytes([i]) * 32) for i in range(max(start, 1), 256))
                if k.endswith(ending))
