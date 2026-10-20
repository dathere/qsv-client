"""A system tray for qsv-clientd (``pip install 'qsv-client[tray]'``, run ``qsv-clientd-tray``).

The tray hosts the daemon on a background thread, or controls one that is already running.
pystray is imported only in :mod:`.app` and Pillow only in :mod:`.icons`, so the rest works
(and is tested) without the extra.
"""
