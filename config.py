"""Watchlist and thresholds. Edit this file to change what the monitor looks for."""

BUDGET = 300.00        # CAD before tax: at or under this counts as "in budget"
ALERT_CAP = 380.00     # CAD: never alert above this
PRICE_FLOOR = 80.00    # CAD: anything cheaper is almost certainly a remote, cable or part

REGION = "AB"          # Best Buy pricing region
POSTAL_PREFIX = "T9H"  # Fort McMurray, used for Best Buy pickup availability

ONKYO_CAVEAT = "Onkyo reliability/warranty support — check seller return policy"
OVER_BUDGET_CAVEAT = "Usually over budget — rare to see under the cap"

# (model, tier, channels, atmos, caveat)
MODELS = [
    # Tier 1
    ("STR-DH790",  1, "7.2", True,  None),
    ("STR-DH590",  1, "5.2", False, None),
    ("AVR-S540BT", 1, "5.2", False, None),
    ("AVR-S570BT", 1, "5.2", False, None),
    ("AVR-S650H",  1, "5.2", False, None),
    ("AVR-S660H",  1, "5.2", False, None),
    ("AVR-S750H",  1, "7.2", True,  None),
    ("AVR-S760H",  1, "7.2", True,  None),
    ("AVR-S770H",  1, "7.2", True,  None),
    ("RX-V385",    1, "5.1", False, None),
    ("RX-V485",    1, "5.1", False, None),
    ("RX-V4A",     1, "5.2", False, None),
    ("RX-V6A",     1, "7.2", True,  None),
    ("VSX-534",    1, "5.2", True,  None),
    ("VSX-535",    1, "5.2", True,  None),
    ("VSX-834",    1, "7.2", True,  None),
    ("VSX-835",    1, "7.2", True,  None),
    # Tier 2
    ("TX-SR393",   2, "5.2", True,  ONKYO_CAVEAT),
    ("TX-SR494",   2, "7.2", True,  ONKYO_CAVEAT),
    ("TX-NR595",   2, "7.2", True,  ONKYO_CAVEAT),
    ("AVR-X1500H", 2, "7.2", True,  OVER_BUDGET_CAVEAT),
    ("AVR-X1600H", 2, "7.2", True,  OVER_BUDGET_CAVEAT),
    ("AVR-X1700H", 2, "7.2", True,  OVER_BUDGET_CAVEAT),
    ("NR1510",     2, "5.2", False, OVER_BUDGET_CAVEAT),
    ("NR1711",     2, "7.2", True,  OVER_BUDGET_CAVEAT),
]

# Titles containing any of these are accessories/parts/wrong product type, even if a model number matches.
EXCLUDE_WORDS = [
    "remote", "cable", "cord", "power supply", "adapter", "board", "replacement",
    "part", "manual", "soundbar", "sound bar", "compatible with", "for denon",
    "for sony", "for yamaha", "for pioneer", "for onkyo", "for marantz", "fuse",
    "antenna", "knob", "faceplate", "transformer",
]

# Polite crawling
MIN_DELAY_SECONDS = 2.0      # between requests to the same host
JITTER_SECONDS = 1.5
TIMEOUT_SECONDS = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

# Error handling
BROKEN_AFTER_FAILURES = 3    # consecutive failed runs before a "source broken" alert
MAX_BACKOFF_RUNS = 8         # a failing source is retried at most every 8 runs (~2.5 h)
BACK_IN_STOCK_HOURS = 24     # re-alert a listing that has been unavailable at least this long
HEARTBEAT_DAYS = 7           # low-priority "still alive" ping
MAX_ALERTS_PER_RUN = 10      # beyond this, send one summary instead
