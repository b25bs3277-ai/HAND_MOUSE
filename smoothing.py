"""
smoothing.py
------------
Makes the cursor steady enough for handwriting.

Two steps, applied to the cursor position (in screen pixels):

1) One Euro filter  (Casiez et al., 2012)
   - When your hand moves SLOWLY it smooths a lot -> no shaking letters.
   - When your hand moves FAST it smooths less    -> no lag.
   Two knobs:
     min_cutoff : smoothing at slow speed. Lower = steadier (but more lag).
     beta       : how quickly smoothing relaxes as speed rises. Higher = less lag.
   We use ONE speed for x and y together, so diagonal strokes are smoothed
   exactly like straight ones (letters keep their shape).

2) "Rope" dead zone
   The cursor is pulled by an invisible rope of length dead_zone_px.
   Tiny wobbles smaller than the rope do nothing at all, but bigger moves
   pull the cursor smoothly (no jumpy steps like a normal dead zone).
"""

import math


def _alpha(cutoff_hz, dt):
    """How much of the new value to take (0..1) for a given cutoff frequency."""
    tau = 1.0 / (2.0 * math.pi * cutoff_hz)
    return 1.0 / (1.0 + tau / dt)


class OneEuro2D:
    def __init__(self, min_cutoff=1.0, beta=0.01, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self):
        self.x = self.y = None      # last filtered position
        self.vx = self.vy = 0.0     # filtered speed (pixels per second)
        self.t = None

    def __call__(self, x, y, t):
        if self.x is None:                       # first point: nothing to smooth yet
            self.x, self.y, self.t = x, y, t
            return x, y
        dt = t - self.t
        if dt <= 1e-4:                           # same moment twice: keep old value
            return self.x, self.y
        self.t = t

        # 1. speed, lightly smoothed
        a_d = _alpha(self.d_cutoff, dt)
        self.vx = a_d * (x - self.x) / dt + (1 - a_d) * self.vx
        self.vy = a_d * (y - self.y) / dt + (1 - a_d) * self.vy
        speed = math.hypot(self.vx, self.vy)

        # 2. faster movement -> higher cutoff -> less smoothing
        cutoff = self.min_cutoff + self.beta * speed
        a = _alpha(cutoff, dt)
        self.x = a * x + (1 - a) * self.x
        self.y = a * y + (1 - a) * self.y
        return self.x, self.y


class PointFilter:
    """One Euro filter + rope dead zone, for the cursor."""

    def __init__(self, min_cutoff=1.0, beta=0.01, d_cutoff=1.0, dead_zone_px=3.0):
        self.euro = OneEuro2D(min_cutoff, beta, d_cutoff)
        self.dead_zone = dead_zone_px
        self.out = None

    def set_params(self, min_cutoff, beta, d_cutoff, dead_zone_px):
        self.euro.min_cutoff = min_cutoff
        self.euro.beta = beta
        self.euro.d_cutoff = d_cutoff
        self.dead_zone = dead_zone_px

    def reset(self):
        """Forget the past (use after the hand was lost, so it does not glide back)."""
        self.euro.reset()
        self.out = None

    def update(self, x, y, t):
        fx, fy = self.euro(x, y, t)
        if self.out is None:
            self.out = (fx, fy)
            return self.out

        ox, oy = self.out
        dx, dy = fx - ox, fy - oy
        dist = math.hypot(dx, dy)
        r = self.dead_zone
        if dist > r:                 # rope is tight: follow, staying r pixels behind
            k = (dist - r) / dist
            self.out = (ox + dx * k, oy + dy * k)
        return self.out
