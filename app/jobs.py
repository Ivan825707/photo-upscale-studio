"""Простий послідовний виконавець завдань (одне фото за раз).

Послідовність навмисна: паралельна інференс на CPU/GPU вкладає ресурси,
підвищує пікове споживання пам'яті і ускладнює показ чесного прогресу.
"""
from __future__ import annotations

import queue
import threading
import traceback


class JobRunner:
    def __init__(self, workers: int = 1) -> None:
        self._queue: "queue.Queue[callable]" = queue.Queue()
        self._threads = [
            threading.Thread(target=self._loop, daemon=True, name=f"upscale-{i}")
            for i in range(workers)
        ]
        for t in self._threads:
            t.start()

    def submit(self, fn) -> None:
        self._queue.put(fn)

    def _loop(self) -> None:
        while True:
            fn = self._queue.get()
            try:
                fn()
            except Exception:
                # завдання само обробляє свої помилки; сюди дійти не повинно
                traceback.print_exc()
            finally:
                self._queue.task_done()
