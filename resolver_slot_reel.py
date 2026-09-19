"""Resolve a data local e o horário de um cron de Reel."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


BRT = ZoneInfo("America/Sao_Paulo")

# Duas tentativas para cada horário. Os minutos 07 e 22 evitam o pico de
# carga do GitHub Actions no começo da hora.
CRONS_REELS = {
    "7 9 * * *": "06:00",
    "22 9 * * *": "06:00",
    "7 11 * * *": "08:00",
    "22 11 * * *": "08:00",
    "7 13 * * *": "10:00",
    "22 13 * * *": "10:00",
    "7 15 * * *": "12:00",
    "22 15 * * *": "12:00",
    "7 17 * * *": "14:00",
    "22 17 * * *": "14:00",
    "7 19 * * *": "16:00",
    "22 19 * * *": "16:00",
    "7 21 * * *": "18:00",
    "22 21 * * *": "18:00",
    "7 22 * * *": "19:00",
    "22 22 * * *": "19:00",
    "7 0 * * *": "21:00",
    "22 0 * * *": "21:00",
    "7 1 * * *": "22:00",
    "22 1 * * *": "22:00",
}


def resolver_slot(
    cron: str, agora_utc: datetime | None = None
) -> tuple[str, str]:
    """Usa a ocorrência nominal do cron, não a hora atrasada do runner."""

    horario_local = CRONS_REELS.get(cron)
    if horario_local is None:
        raise ValueError(f"Cron não reconhecido: {cron}")

    agora = agora_utc or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        raise ValueError("agora_utc precisa ter fuso horário.")
    agora = agora.astimezone(timezone.utc)
    minuto_texto, hora_texto, *_ = cron.split()
    ocorrencia = agora.replace(
        hour=int(hora_texto),
        minute=int(minuto_texto),
        second=0,
        microsecond=0,
    )
    if ocorrencia > agora:
        ocorrencia -= timedelta(days=1)

    data_local = ocorrencia.astimezone(BRT).date().isoformat()
    return data_local, horario_local


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python resolver_slot_reel.py '<cron>'")
    try:
        data, horario = resolver_slot(sys.argv[1])
    except ValueError as erro:
        raise SystemExit(str(erro)) from erro
    print(f"DATA_PUBLICACAO={data}")
    print(f"HORARIO_PUBLICACAO={horario}")


if __name__ == "__main__":
    main()
