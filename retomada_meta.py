"""Transições conservadoras para sessões de publicação da Meta.

IDs só são abandonados quando a própria Meta informa uma falha conclusiva. Erros
de rede, timeouts e respostas ambíguas preservam o ID ativo para reconciliação.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Callable

from meta_comum import BRT


FALHAS_CONCLUSIVAS = {"error", "failed", "expired"}


def video_facebook_falhou(info: Mapping[str, object]) -> bool:
    status = info.get("status")
    if not isinstance(status, Mapping):
        return False
    valores = [str(status.get("video_status", "")).strip().casefold()]
    for fase in ("uploading_phase", "processing_phase", "publishing_phase"):
        detalhe = status.get(fase)
        if isinstance(detalhe, Mapping):
            valores.append(str(detalhe.get("status", "")).strip().casefold())
    return any(valor in FALHAS_CONCLUSIVAS for valor in valores)


def abandonar_identificador(
    estado: dict,
    *,
    campo_id: str,
    chave_historico: str,
    identificador: str,
    motivo: str,
    info_meta: Mapping[str, object] | None,
    persistir: Callable[[], None],
) -> None:
    """Arquiva um ID conclusivamente morto antes de permitir uma nova sessão."""

    historico = estado.setdefault(chave_historico, [])
    if not isinstance(historico, list):
        raise RuntimeError(f"{chave_historico} precisa ser uma lista antes da retomada.")
    agora = datetime.now(BRT).isoformat()
    if not any(
        isinstance(item, Mapping) and str(item.get("id") or "") == identificador
        for item in historico
    ):
        historico.append(
            {
                "id": identificador,
                "abandonado_em": agora,
                "motivo": motivo[:500],
                "estado_meta": json.dumps(
                    dict(info_meta or {}), ensure_ascii=False, sort_keys=True
                )[:1000],
            }
        )
    estado.pop(campo_id, None)
    if campo_id == "video_id":
        estado.pop("upload_url", None)
    estado.pop("reconciliacao_manual", None)
    estado.update(
        {
            "status": "erro",
            "fase": "identificador_abandonado",
            "erro": f"{motivo}; identificador arquivado antes de nova sessão",
            "ultima_tentativa_em": agora,
        }
    )
    persistir()


def marcar_reconciliacao_manual(
    estado: dict, *, motivo: str, persistir: Callable[[], None]
) -> None:
    """Torna durável um sucesso remoto sem ID local antes de bloquear cleanup."""

    estado.update(
        {
            "status": "processando",
            "fase": "publicado_sem_media_id",
            "reconciliacao_manual": {
                "necessaria": True,
                "motivo": motivo[:500],
                "detectada_em": datetime.now(BRT).isoformat(),
            },
        }
    )
    persistir()
