"""Edit this list to control which configs run alongside live each refresh.

To add a shadow: append a ModelConfig; restart `kalshi_temp serve`.
To remove a shadow: remove it from the list; restart.
"""
from lab.configs import LIVE_MINUS_NWS, LIVE_MINUS_PUSH, LIVE_MINUS_TRUNC


ACTIVE_SHADOWS = [LIVE_MINUS_NWS, LIVE_MINUS_PUSH, LIVE_MINUS_TRUNC]
