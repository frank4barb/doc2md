#!/usr/bin/env bash
# visore.sh - avvia il visore di doc2md in sottofondo e apre la pagina nel browser predefinito.
#
#   ./visore.sh          avvia (se non e' gia' acceso) e apre il browser
#   ./visore.sh stop     spegne il visore (una conversione in corso finisce il documento e si ferma)
#   ./visore.sh stato    dice se e' acceso
#
# Host e porta vengono da doc2md.ini ([visore]). Il registro del visore e il file con il suo pid
# stanno in RAM ($XDG_RUNTIME_DIR, di solito /run/user/<uid>) e spariscono allo spegnimento.
set -euo pipefail

QUI="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PY="$QUI/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
RUN="${XDG_RUNTIME_DIR:-/tmp}/doc2md"
PIDFILE="$RUN/visore.pid"
LOG="$RUN/visore.log"
mkdir -p "$RUN"

read -r HOST PORTA < <("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import config
print(config.valore("visore", "host"), config.valore("visore", "porta"))' "$QUI")
URL="http://$HOST:$PORTA/"

acceso() { curl -fs -o /dev/null --max-time 2 "${URL}api/radici"; }
pid() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && cat "$PIDFILE"; }

apri_browser() {
  if command -v xdg-open >/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    xdg-open "$URL" >/dev/null 2>&1 &
  else
    echo "nessuno schermo grafico qui: apri $URL nel browser (da un altro PC: ssh -L $PORTA:127.0.0.1:$PORTA ...)"
  fi
}

case "${1:-avvia}" in
  stop)
    if p=$(pid); then
      kill -TERM "$p"
      for _ in $(seq 1 50); do kill -0 "$p" 2>/dev/null || break; sleep 0.2; done
      rm -f "$PIDFILE"
      echo "visore spento"
    elif acceso; then
      echo "il visore risponde su $URL ma non l'ha avviato questo script: chiudilo dal suo terminale (Ctrl+C)"
    else
      echo "visore gia' spento"
    fi
    ;;
  stato)
    if acceso; then p=$(pid || true); echo "acceso su $URL${p:+ (pid $p)}"; else echo "spento"; fi
    ;;
  avvia)
    if ! acceso; then
      cd "$QUI"
      setsid nohup "$PY" visore.py >"$LOG" 2>&1 < /dev/null &
      echo $! > "$PIDFILE"
      for _ in $(seq 1 100); do
        acceso && break
        if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
          echo "il visore non e' partito:"; tail -n 20 "$LOG"; rm -f "$PIDFILE"; exit 1
        fi
        sleep 0.2
      done
      acceso || { echo "il visore non risponde dopo 20 s; registro: $LOG"; exit 1; }
      echo "visore acceso su $URL (spegnimento: $0 stop)"
    else
      echo "visore gia' acceso su $URL"
    fi
    apri_browser
    ;;
  *)
    echo "uso: $0 [avvia|stop|stato]"; exit 2
    ;;
esac
