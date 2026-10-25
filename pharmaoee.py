# -*- coding: utf-8 -*-
"""
PharmaOEE — учебное настольное приложение для мониторинга OEE и учёта простоев.

Запуск:  python pharmaoee.py            (Windows: py pharmaoee.py)
Проверка без окна:  python pharmaoee.py --check
Важно: это учебный прототип, не валидированная промышленная или GMP-система.
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("PharmaOEE требует Python 3.10 или новее. Установлена версия " + sys.version.split()[0])

from pharmaoee_app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
