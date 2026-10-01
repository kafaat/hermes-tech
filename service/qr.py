"""A QR code for the business's WhatsApp link, made here (spec 28.18): no online generator, no redirect service.

    link = wa_me(phone="967712345678", text="مرحبا، أريد الطلب")   # https://wa.me/967712345678?text=…
    svg  = qr_svg(link)                                            # printable SVG: tables, menus, the shop door

Online QR generators often encode their own short link that redirects to the target; when the subscription lapses the
printed codes stop working, and every scan passes through a third party. This encodes the wa.me link itself.

ISO/IEC 18004, byte mode, error correction level M (about 15% of the code may be damaged), versions 1 to 10 (up to
213 bytes, enough for wa.me with a short greeting). The eight masks are scored with the standard penalty rules and the
lowest is used. tests/test_service_qr.py holds reference matrices produced by an independent encoder (segno).
"""
from __future__ import annotations
import re
from urllib.parse import quote

# version -> (total codewords, EC codewords per block, blocks) at level M
VERSIONS = {1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2), 5: (134, 24, 2), 6: (172, 16, 4),
            7: (196, 18, 4), 8: (242, 22, 4), 9: (292, 22, 5), 10: (346, 26, 5)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}
EC_M_BITS = 0                                   # format bits for level M


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(payload: bytes) -> tuple[int, list[int]]:
    for version, (total, ec_len, blocks) in VERSIONS.items():
        data_cap = total - ec_len * blocks
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(payload) <= data_cap * 8:
            break
    else:
        raise ValueError("too long for a QR code up to version 10 (213 bytes)")
    bits = [0, 1, 0, 0] + [(len(payload) >> i) & 1 for i in reversed(range(count_bits))]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, data_cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < data_cap:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    short_len = total // blocks                  # codewords per short block, EC included
    long_count = total % blocks
    divisor = _rs_divisor(ec_len)
    data_blocks, ec_blocks, k = [], [], 0
    for i in range(blocks):
        n = short_len - ec_len + (1 if i >= blocks - long_count else 0)
        block = data[k:k + n]
        k += n
        data_blocks.append(block)
        ec_blocks.append(_rs_remainder(block, divisor))
    out = []
    for i in range(max(len(b) for b in data_blocks)):
        out += [b[i] for b in data_blocks if i < len(b)]
    for i in range(ec_len):
        out += [b[i] for b in ec_blocks]
    return version, out


class _Grid:
    def __init__(self, version: int):
        self.v, self.size = version, version * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool):
        self.m[y][x], self.fn[y][x] = dark, True

    def functions(self):
        s = self.size
        for i in range(s):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = ALIGN[self.v]
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)
        if self.v >= 7:
            rem = self.v
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.v << 12 | rem
            for i in range(18):
                bit, a, b = (bits >> i) & 1 == 1, s - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)

    def format_bits(self, mask: int):
        data = EC_M_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1                    # noqa: E731
        s = self.size
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, s - 15 + i, bit(i))
        self.set(8, s - 8, True)

    def place(self, codewords: list[int]):
        s, i, right = self.size, 0, self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    y = s - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < len(codewords) * 8:
                        self.m[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int):
        cond = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
                lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
                lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
                lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and cond(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self) -> int:
        """ISO/IEC 18004:2015 7.8.3: N1 runs of five or more, N2 2x2 blocks, N3 the 1:1:3:1:1 finder-like pattern with
        four light modules (or the symbol edge) on one side, N4 the share of dark modules."""
        s, m, score = self.size, self.m, 0
        lines = [m[y] for y in range(s)] + [[m[y][x] for y in range(s)] for x in range(s)]
        pattern = [True, False, True, True, True, False, True]
        for line in lines:
            run, prev = 0, None
            for v in line + [None]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, v
            i = 0
            while i <= s - 7:
                if line[i:i + 7] == pattern:
                    if i in (0, s - 7) or not any(line[max(i - 4, 0):i]) or not any(line[i + 7:i + 11]):
                        score += 40
                        i += 7
                    else:
                        i += 4
                    continue
                i += 1
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(r) for r in m)
        score += 10 * int(abs(dark * 100 / (s * s) - 50) / 5)
        return score


def matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """The module matrix (True = dark) for text, UTF-8, level M; the lowest-penalty mask unless one is given."""
    version, cw = _codewords(text.encode("utf-8"))
    best = None
    for k in (range(8) if mask is None else [mask]):
        g = _Grid(version)
        g.functions()
        g.place(cw)
        g.apply_mask(k)
        g.format_bits(k)
        p = g.penalty()
        if best is None or p < best[0]:
            best = (p, g)
    return best[1].m


def qr_svg(text: str, module_px: int = 8, quiet: int = 4) -> str:
    """A self-contained SVG: one path of dark squares on a white background with the 4-module quiet zone."""
    m = matrix(text)
    n = len(m) + 2 * quiet
    d = "".join(f"M{x + quiet} {y + quiet}h1v1h-1z" for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * module_px}" height="{n * module_px}"'
            f' shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="#fff"/><path d="{d}" fill="#000"/></svg>')


def wa_me(phone: str, text: str | None = None) -> str:
    """https://wa.me/<international number, digits only>[?text=…]; the number as WhatsApp expects it, no + or 00."""
    digits = re.sub(r"[\s-]", "", str(phone or ""))
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("00"):
        digits = digits[2:]
    if not re.fullmatch(r"[1-9]\d{7,14}", digits):
        raise ValueError("an international number in digits, e.g. 967712345678")
    link = f"https://wa.me/{digits}"
    if text:
        if len(text) > 120:
            raise ValueError("a greeting of at most 120 characters")
        link += "?text=" + quote(text, safe="")
    return link
