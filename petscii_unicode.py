"""Unicode approximations of PET screen codes, for the server's terminal view.

Letters, digits and punctuation map directly. Graphics characters map to the
Unicode block, line or shade character whose 8x8 pixel pattern is closest to
the glyph in the PET character set (convert.PET_GLYPHS). Screen codes 128-255 are the same glyphs in
reverse video; the terminal view shows them with inverse video.
"""
import numpy as np

import convert


def _mask(fn):
    m = np.zeros((8, 8), dtype=np.float32)
    for y in range(8):
        for x in range(8):
            m[y, x] = 1.0 if fn(x, y) else 0.0
    return m


CANDIDATES = {
    " ": lambda x, y: False,
    "█": lambda x, y: True,
    "▀": lambda x, y: y < 4,
    "▄": lambda x, y: y >= 4,
    "▌": lambda x, y: x < 4,
    "▐": lambda x, y: x >= 4,
    "▘": lambda x, y: x < 4 and y < 4,
    "▝": lambda x, y: x >= 4 and y < 4,
    "▖": lambda x, y: x < 4 and y >= 4,
    "▗": lambda x, y: x >= 4 and y >= 4,
    "▚": lambda x, y: (x < 4) == (y < 4),
    "▞": lambda x, y: (x < 4) != (y < 4),
    "▙": lambda x, y: not (x >= 4 and y < 4),
    "▛": lambda x, y: not (x >= 4 and y >= 4),
    "▜": lambda x, y: not (x < 4 and y >= 4),
    "▟": lambda x, y: not (x < 4 and y < 4),
    "▔": lambda x, y: y == 0,
    "▁": lambda x, y: y == 7,
    "▂": lambda x, y: y >= 6,
    "▃": lambda x, y: y >= 5,
    "▅": lambda x, y: y >= 3,
    "▆": lambda x, y: y >= 2,
    "▇": lambda x, y: y >= 1,
    "▏": lambda x, y: x == 0,
    "▎": lambda x, y: x <= 1,
    "▍": lambda x, y: x <= 2,
    "▋": lambda x, y: x <= 4,
    "▊": lambda x, y: x <= 5,
    "▉": lambda x, y: x <= 6,
    "▕": lambda x, y: x == 7,
    "▒": lambda x, y: (x + y) % 2 == 0,
    "░": lambda x, y: x % 2 == 0 and y % 2 == 0,
    "▓": lambda x, y: not (x % 2 == 1 and y % 2 == 1),
    "─": lambda x, y: y in (3, 4),
    "│": lambda x, y: x in (3, 4),
    "┼": lambda x, y: y in (3, 4) or x in (3, 4),
    "├": lambda x, y: x in (3, 4) or (y in (3, 4) and x >= 3),
    "┤": lambda x, y: x in (3, 4) or (y in (3, 4) and x <= 4),
    "┬": lambda x, y: y in (3, 4) or (x in (3, 4) and y >= 3),
    "┴": lambda x, y: y in (3, 4) or (x in (3, 4) and y <= 4),
    "┌": lambda x, y: (y in (3, 4) and x >= 3) or (x in (3, 4) and y >= 3),
    "┐": lambda x, y: (y in (3, 4) and x <= 4) or (x in (3, 4) and y >= 3),
    "└": lambda x, y: (y in (3, 4) and x >= 3) or (x in (3, 4) and y <= 4),
    "┘": lambda x, y: (y in (3, 4) and x <= 4) or (x in (3, 4) and y <= 4),
    "╱": lambda x, y: abs((7 - x) - y) <= 0,
    "╲": lambda x, y: abs(x - y) <= 0,
    "╳": lambda x, y: x == y or 7 - x == y,
    "●": lambda x, y: (x - 3.5) ** 2 + (y - 3.5) ** 2 <= 10,
    "○": lambda x, y: 6 <= (x - 3.5) ** 2 + (y - 3.5) ** 2 <= 13,
}
# one-pixel lines and corners, and triangles: (character, pixel test)
EXTRA = []
for r in range(8):
    EXTRA.append(("▔" if r == 0 else "▁" if r == 7 else "─", lambda x, y, r=r: y == r))
    EXTRA.append(("▏" if r == 0 else "▕" if r == 7 else "│", lambda x, y, r=r: x == r))
EXTRA += [
    ("╭", lambda x, y: (y == 4 and x >= 4) or (x == 4 and y >= 4)),
    ("╮", lambda x, y: (y == 4 and x <= 4) or (x == 4 and y >= 4)),
    ("╰", lambda x, y: (y == 4 and x >= 4) or (x == 4 and y <= 4)),
    ("╯", lambda x, y: (y == 4 and x <= 4) or (x == 4 and y <= 4)),
    ("┼", lambda x, y: y == 4 or x == 4),
    ("├", lambda x, y: x == 4 or (y == 4 and x >= 4)),
    ("┤", lambda x, y: x == 4 or (y == 4 and x <= 4)),
    ("┬", lambda x, y: y == 4 or (x == 4 and y >= 4)),
    ("┴", lambda x, y: y == 4 or (x == 4 and y <= 4)),
    ("┌", lambda x, y: (y == 0 and x >= 0) or (x == 0)),
    ("┐", lambda x, y: (y == 0) or (x == 7)),
    ("└", lambda x, y: (y == 7) or (x == 0)),
    ("┘", lambda x, y: (y == 7) or (x == 7)),
    ("◤", lambda x, y: x + y <= 7),
    ("◥", lambda x, y: x >= y),
    ("◣", lambda x, y: x <= y),
    ("◢", lambda x, y: x + y >= 7),
    ("╱", lambda x, y: abs((7 - x) - y) <= 1),
    ("╲", lambda x, y: abs(x - y) <= 1),
]
_CANDS = [(ch, _mask(fn)) for ch, fn in CANDIDATES.items()] + \
         [(ch, _mask(fn)) for ch, fn in EXTRA]

# Characters that pixel matching cannot find, by screen code (PET graphics set)
FIXED = {0: "@", 27: "[", 28: "£", 29: "]", 30: "↑", 31: "←",
         65: "♠", 83: "♥", 88: "♣", 90: "♦", 92: "▒", 94: "π", 104: "▒"}


def build_map(rom=None):
    """Return a list of 128 strings: the Unicode character for each screen code."""
    glyphs, _ = convert.load_glyphs(rom, allow_reverse=False)
    out = []
    for code in range(128):
        if code in FIXED:
            out.append(FIXED[code])
        elif 1 <= code <= 26:
            out.append(chr(64 + code))
        elif 32 <= code <= 63:
            out.append(chr(code))
        else:
            g = glyphs[code].reshape(8, 8) / 255.0
            out.append(min(_CANDS, key=lambda c: float(((c[1] - g) ** 2).sum()))[0])
    return out


if __name__ == "__main__":
    m = build_map()
    for row in range(0, 128, 16):
        print(f"{row:3d} " + " ".join(m[row:row + 16]))
