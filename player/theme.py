"""Warm amber/rust theme.

Textual's default `textual-dark` drives the header, footer key hints, row
cursor and focus borders from `$primary` (#0178D4), so replacing that variable
does most of the work; the CSS in app.py pins the handful of places that would
otherwise pick a colour of their own.

The list itself is deliberately near-neutral. Amber marks *where you are*
(cursor, focus, progress) and rust marks *what is playing*, so the two never
compete — the ▶ row stays findable even when the cursor is elsewhere.
"""

from textual.theme import Theme

AMBER = "#E8913A"  # cursor, focus, progress — "where you are"
RUST = "#C4541F"  # the playing track — "what is playing"
PARCHMENT = "#E6DFD5"
MUTED = "#8A7F73"

PLAYER_THEME = Theme(
    name="player",
    primary=AMBER,
    secondary=RUST,
    accent=AMBER,
    foreground=PARCHMENT,
    background="#14110F",
    surface="#1C1815",
    panel="#241F1A",
    boost="#FFFFFF08",  # zebra stripes: a hint of lift, no hue
    success="#8FA05A",
    warning=AMBER,
    error="#B34A32",
    dark=True,
    variables={
        "block-cursor-foreground": "#14110F",
        "block-cursor-background": AMBER,
        "block-cursor-text-style": "bold",
        "footer-key-foreground": AMBER,
        "footer-description-foreground": MUTED,
        "border": AMBER,
        "border-blurred": "#3A322B",
    },
)
