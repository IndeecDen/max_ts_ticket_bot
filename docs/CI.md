# Автоматические проверки GitHub

Workflow `.github/workflows/ci.yml` запускается на push, pull request и вручную через вкладку Actions. Код размещён в ветке `release/1.0.1-candidate` репозитория [IndeecDen/max_ts_ticket_bot](https://github.com/IndeecDen/max_ts_ticket_bot). Первый запуск на 214 тестах прошёл во всех четырёх сочетаниях ОС/Python; результаты новых изменений проверяйте в Actions.

Матрица запускает полный `unittest` на Python 3.12 и 3.13 в Ubuntu 24.04 и Windows. Зависимости берутся из `requirements.txt`, после установки выполняется `pip check`. Незакрытые ресурсы проверяются с `-W error::ResourceWarning`.

Отдельное задание проверяет синтаксис и `--help` всех Bash-скриптов, затем шаблоны service/timer через `systemd-analyze verify`. В **временных копиях** unit команда `ExecStart` заменяется на `/usr/bin/true`, поскольку установленных путей бота в CI нет. Это проверка директив и связей unit, а не запуска бота или доступности его файлов.

Тесты используют временные базы, локальные HTTP-серверы и имитацию MAX. Токен MAX и production-профиль для CI не нужны. Workflow не устанавливает службу, не публикует проект и не выполняет развёртывание. Права workflow ограничены `contents: read`; используется обычное событие `pull_request`.

Повторить проверки локально:

```bash
python -m pip check
python -W error::ResourceWarning -m unittest discover -s tests -v
bash scripts/check-deploy.sh
```

Последняя команда требует Linux с `systemd-analyze`. На Windows через Git Bash можно отдельно выполнить `bash -n scripts/install.sh` и аналогично проверить остальные скрипты.

Зелёный CI не заменяет установку и обновление на чистых Debian 13/Ubuntu 24.04, проверку прав systemd, перезагрузку, HTTPS и живой пилот в MAX. До первого запуска Actions результаты Linux/Python 3.13 не считаются подтверждёнными.

Настройки официальных actions сверены с документацией [checkout](https://github.com/actions/checkout) и [setup-python](https://github.com/actions/setup-python).
