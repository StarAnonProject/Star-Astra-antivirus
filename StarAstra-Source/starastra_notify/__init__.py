"""StarAstra notify — панель уведомлений об угрозах (in-app cards + Windows toasts)."""
from starastra_notify.panel import (  # noqa: F401
    NotifyPanel,
    ToastPoller,
    make_system_notifier,
    MAX_CARDS,
    POLL_SEC,
)
