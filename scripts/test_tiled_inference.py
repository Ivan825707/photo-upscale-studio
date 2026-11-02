"""Швидкий юніт-мапінг _tiled_inference з фейковою сесією nearest-повторення."""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
sys.path.insert(0, ".")
from app import engine


class FakeSess:
    """Модель, що повторює кожен піксель s×s (детермінований мапінг)."""

    def __init__(self, s: int) -> None:
        self.s = s

    def run(self, _outs, feeds):
        x = next(iter(feeds.values()))              # (1, 3, T, T)
        y = np.repeat(np.repeat(x, self.s, axis=2), self.s, axis=3)
        return [y]


CASES = [
    (100, 130, 4, 512, 24),
    (1000, 900, 2, 384, 24),
    (464, 464, 4, 512, 24),
    (938, 938, 4, 512, 24),
    (7, 5, 4, 64, 16),
    (33, 33, 4, 64, 16),
    (513, 700, 4, 96, 24),
]

fails = 0
for (H, W, s, tile, ov) in CASES:
    rng = np.random.default_rng(H * 1009 + W * 17 + s)
    src = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
    sess = FakeSess(s)
    got = engine._tiled_inference(src, sess, "x", "y", s, tile, ov,
                                  lambda p, t: None)
    want = np.repeat(np.repeat(src, s, axis=0), s, axis=1)
    ok = np.array_equal(got, want)
    print(f"H={H:4} W={W:4} s={s} tile={tile} core={tile - 2*ov:3} "
          f"→ {'OK' if ok else 'FAIL'} shape={got.shape}")
    if not ok:
        fails += 1
        d = np.argwhere(got != want)
        y, x = d[0][:2]
        print(f"   перший розбіг: ({y},{x}) got={got[y, x, 0]} "
              f"want={want[y, x, 0]}")
print("РЕЗУЛЬТАТ:", "all OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
