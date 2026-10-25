import logging

# Ожидаемые в тестах сообщения об ошибках не выводятся в консоль.
logging.getLogger().addHandler(logging.NullHandler())
