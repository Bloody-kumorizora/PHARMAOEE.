#!/bin/sh
# Запуск PharmaOEE в Linux / RED OS. Нужен Python 3.10+ с Tkinter.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не найден. Установите python3 (например: sudo dnf install python3)."
  exit 1
fi
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "Модуль Tkinter не найден. Установите его, например: sudo dnf install python3-tkinter"
  echo "или sudo apt install python3-tk (имя пакета зависит от дистрибутива)."
  exit 1
fi
exec python3 pharmaoee.py "$@"
