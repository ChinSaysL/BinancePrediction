"""Out-of-band notifications.

Currently one transport: Telegram, in `btcpredict.notify.telegram`. It is
deliberately isolated from the trading code -- `main.py` calls high-level event
methods (`entry`, `entry_failed`, `exit`, `settled`) and never builds an HTTP
payload itself.
"""

from btcpredict.notify.telegram import TelegramNotifier

__all__ = ["TelegramNotifier"]
