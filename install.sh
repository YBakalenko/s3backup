#!/bin/sh
# Install only this tool and its prerequisites; never upgrade the router wholesale.
# shellcheck disable=SC2015
set -eu
umask 077
S3_DOWNLOAD=0
case "${1:-}" in
  '') [ "$#" = 0 ] || { echo 'Неизвестный аргумент.' >&2; exit 2; };;
  --download) [ "$#" = 1 ] || { echo 'Использование: install.sh [--download]' >&2; exit 2; }; S3_DOWNLOAD=1;;
  --help|-h) printf 'Установка из локального каталога: sh install.sh\nЗагрузка из GitHub и установка: sh install.sh --download\n'; exit 0;;
  *) echo 'Использование: install.sh [--download]' >&2; exit 2;;
esac
S3_PREFIX=${S3BACKUP_PREFIX:-/opt}
case "$S3_PREFIX" in /*) ;; *) echo 'Некорректный путь установки.' >&2; exit 1;; esac
case "$S3_PREFIX" in *[!a-zA-Z0-9_./-]*|*/../*|*/..|/) echo 'Некорректный путь установки.' >&2; exit 1;; esac
[ "$(id -u)" = 0 ] || { echo 'Установка запускается от root в Entware.' >&2; exit 1; }
S3_SOURCE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PATH="$S3_PREFIX/bin:$S3_PREFIX/sbin:$PATH"
export PATH
s3_no_links() {
  S3_PATH=$1
  while [ "$S3_PATH" != / ]; do
    [ ! -L "$S3_PATH" ] || { echo 'Отказ: ссылка в пути установки.' >&2; exit 1; }
    S3_PATH=${S3_PATH%/*}; [ -n "$S3_PATH" ] || S3_PATH=/
  done
}
for S3_PATH_ITEM in "$S3_PREFIX/bin" "$S3_PREFIX/etc/cron.d" "$S3_PREFIX/etc/init.d" "$S3_PREFIX/var/run/s3backup"; do s3_no_links "$S3_PATH_ITEM"; done
for S3_PATH_ITEM in "$S3_PREFIX/bin/.s3backup.new" "$S3_PREFIX/etc/init.d/.S98s3backup.new" "$S3_PREFIX/etc/cron.d/.s3backup.new" "$S3_PREFIX/var/run/s3backup/control.lock" "$S3_PREFIX/var/run/s3backup/run.lock"; do s3_no_links "$S3_PATH_ITEM"; done

S3_DOWNLOAD_DIR=''
s3_cleanup() {
  if [ -n "$S3_DOWNLOAD_DIR" ] && [ -d "$S3_DOWNLOAD_DIR" ] && [ ! -L "$S3_DOWNLOAD_DIR" ]; then
    rm -rf -- "$S3_DOWNLOAD_DIR"
  fi
}
trap s3_cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
if [ "$S3_DOWNLOAD" = 1 ]; then
  command -v curl >/dev/null 2>&1 || { echo 'Для --download нужен curl. Установите его в Entware.' >&2; exit 1; }
  S3_DOWNLOAD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/s3backup-install.XXXXXX")
  chmod 700 "$S3_DOWNLOAD_DIR"
  S3_SOURCE=$S3_DOWNLOAD_DIR
  S3_BASE_URL='https://raw.githubusercontent.com/YBakalenko/s3backup/main'
  for S3_FILE in s3backup S98s3backup; do
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 20 --max-time 120 --output "$S3_SOURCE/$S3_FILE" "$S3_BASE_URL/$S3_FILE" || { echo 'Загрузка не удалась; установка отменена.' >&2; exit 1; }
    [ -s "$S3_SOURCE/$S3_FILE" ] || { echo 'Загружен пустой файл; установка отменена.' >&2; exit 1; }
    case "$(head -n 1 "$S3_SOURCE/$S3_FILE")" in '#!'*) ;; *) echo 'Получен некорректный файл; установка отменена.' >&2; exit 1;; esac
  done
fi
[ -f "$S3_SOURCE/s3backup" ] && [ -f "$S3_SOURCE/S98s3backup" ] || { echo 'Скопируйте весь каталог утилиты или используйте --download.' >&2; exit 1; }
command -v opkg >/dev/null 2>&1 || { echo 'Сначала установите Entware.' >&2; exit 1; }
printf 'Установить s3backup и необходимые пакеты? Архивы и настройки сохраняются. [да/НЕТ]: ' >/dev/tty
read -r S3_ANSWER </dev/tty
case "$S3_ANSWER" in да|yes|y) ;; *) exit 0;; esac
opkg update
opkg install bash rclone jq cron flock blkid coreutils-sha256sum coreutils-stat ca-bundle
mkdir -p "$S3_PREFIX/bin" "$S3_PREFIX/etc/cron.d" "$S3_PREFIX/etc/init.d" "$S3_PREFIX/var/run/s3backup"
exec 8> "$S3_PREFIX/var/run/s3backup/control.lock"
flock -n 8 || { echo 'Другая настройка уже выполняется.' >&2; exit 1; }
exec 9> "$S3_PREFIX/var/run/s3backup/run.lock"
flock -n 9 || { echo 'Остановите текущую загрузку перед установкой.' >&2; exit 1; }
sed "1s|.*|#!$S3_PREFIX/bin/bash|" "$S3_SOURCE/s3backup" > "$S3_PREFIX/bin/.s3backup.new"
chmod 755 "$S3_PREFIX/bin/.s3backup.new"
mv -f "$S3_PREFIX/bin/.s3backup.new" "$S3_PREFIX/bin/s3backup"
sed "s|@PREFIX@|$S3_PREFIX|g" "$S3_SOURCE/S98s3backup" > "$S3_PREFIX/etc/init.d/.S98s3backup.new"
chmod 755 "$S3_PREFIX/etc/init.d/.S98s3backup.new"
mv -f "$S3_PREFIX/etc/init.d/.S98s3backup.new" "$S3_PREFIX/etc/init.d/S98s3backup"
# Tick checks local time/state only. S3 is contacted once per configured day.
printf 'SHELL=/bin/sh\nPATH=%s/bin:%s/sbin:/usr/sbin:/usr/bin:/sbin:/bin\n* * * * * root %s/bin/s3backup tick >/dev/null 2>&1\n' "$S3_PREFIX" "$S3_PREFIX" "$S3_PREFIX" > "$S3_PREFIX/etc/cron.d/.s3backup.new"
chmod 600 "$S3_PREFIX/etc/cron.d/.s3backup.new"
mv -f "$S3_PREFIX/etc/cron.d/.s3backup.new" "$S3_PREFIX/etc/cron.d/s3backup"
flock -u 9
flock -u 8
exec 9>&-
exec 8>&-
if [ -x "$S3_PREFIX/etc/init.d/S10cron" ]; then "$S3_PREFIX/etc/init.d/S10cron" start; fi
"$S3_PREFIX/bin/s3backup" configure
if [ ! -f "$S3_PREFIX/etc/s3backup/config.json" ]; then
  echo 'Программа установлена, настройка не завершена. Продолжить: s3backup configure'
  exit 0
fi
printf 'Включить ежедневное копирование? [да/НЕТ]: ' >/dev/tty
read -r S3_ANSWER </dev/tty
case "$S3_ANSWER" in да|yes|y) "$S3_PREFIX/bin/s3backup" start;; *) echo 'Настроено. Включение: s3backup start';; esac
