"""Publica um único Reel no slot exato informado pelo workflow."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import requests

from meta_comum import (
    MAX_SUBSTITUICOES_POR_RODADA,
    STATUS_FINAIS,
    esgotou_tentativas,
    pular_e_puxar_proximo,
    BRT,
    PLATAFORMAS,
    ROOT,
    aguardar_facebook_publicado,
    aguardar_instagram,
    alvo_exato,
    baixar_midia,
    consultar_container_instagram,
    consultar_video_facebook,
    executar_plataforma,
    graph_post,
    obrigatoria,
    persistir_fila,
    publicacao_facebook_em_andamento,
    reconciliar_instagram_publicado,
    registrar_publicado,
    sanitizar_texto,
    token_pagina,
    upload_facebook_completo,
    validar_fila_operacional,
    validar_upload_url_meta,
    verificar_midia_remota,
    video_facebook_publicado,
)
from retomada_meta import (
    abandonar_identificador,
    marcar_reconciliacao_manual,
    video_facebook_falhou,
)

# O conferidor da fila agora só AVISA sobre item ruim, para um vídeo errado lá no
# fim da fila não calar o canal inteiro. Quem barra o item ruim é aqui, na hora
# de publicar, e só o horário dele fica sem publicar (incidente de 12/09/2026).
from validar_filas import defeito_do_reel


FILA_FILE = ROOT / "fila" / "fila-reels.json"


def pendencias_anteriores(
    colecao: list[dict], data: str, horario: str
) -> list[dict]:
    """Lista itens não finalizados anteriores ao slot solicitado."""

    alvo = (data, horario)
    return [
        item
        for item in colecao
        if isinstance(item.get("data"), str)
        and isinstance(item.get("horario"), str)
        and (item["data"], item["horario"]) < alvo
        and item.get("status") not in STATUS_FINAIS
    ]


def _aguardar_instagram_seguro(
    container_id: str, token: str, estado: dict, persistir
) -> str | None:
    try:
        return aguardar_instagram(container_id, token)
    except Exception as falha:
        try:
            info = consultar_container_instagram(container_id, token)
        except Exception:
            raise falha
        codigo = str(info.get("status_code") or "").upper()
        if codigo in {"ERROR", "EXPIRED"}:
            abandonar_identificador(
                estado,
                campo_id="container_id",
                chave_historico="containers_abandonados",
                identificador=container_id,
                motivo=f"Instagram {codigo}",
                info_meta=info,
                persistir=persistir,
            )
            return None
        raise falha


def _aguardar_facebook_seguro(
    video_id: str, token: str, estado: dict, persistir
) -> None:
    try:
        aguardar_facebook_publicado(video_id, token)
    except Exception as falha:
        try:
            info = consultar_video_facebook(video_id, token)
        except Exception:
            raise falha
        if video_facebook_publicado(info):
            return
        if video_facebook_falhou(info):
            abandonar_identificador(
                estado,
                campo_id="video_id",
                chave_historico="videos_abandonados",
                identificador=video_id,
                motivo="Facebook informou falha conclusiva",
                info_meta=info,
                persistir=persistir,
            )
        raise falha


def publicar_instagram(item: dict, estado: dict, persistir) -> str:
    token = obrigatoria("IG_ACCESS_TOKEN")
    ig_id = obrigatoria("IG_BUSINESS_ID")
    container_id = estado.get("container_id")
    codigo: str | None = None
    if container_id:
        codigo = _aguardar_instagram_seguro(
            str(container_id), token, estado, persistir
        )
        if codigo is None:
            container_id = None
    if not container_id:
        # O Instagram recebe a URL diretamente. Só dependemos da Release ao
        # criar um container novo; um ID persistido é reconciliado primeiro.
        verificar_midia_remota(item["midia"])
        resposta = graph_post(
            f"{ig_id}/media",
            {
                "media_type": "REELS",
                "video_url": item["midia"]["url_publica"],
                "caption": estado["legenda"],
                "share_to_feed": str(estado.get("share_to_feed", True)).lower(),
                "access_token": token,
            },
        )
        container_id = resposta.get("id")
        if not container_id:
            raise RuntimeError(f"Container do Instagram sem ID: {resposta}")
        estado.update(
            {
                "status": "processando",
                "fase": "container_criado",
                "container_id": str(container_id),
            }
        )
        persistir()
        codigo = _aguardar_instagram_seguro(
            str(container_id), token, estado, persistir
        )
        if codigo is None:
            raise RuntimeError(
                "O novo container do Instagram falhou conclusivamente; "
                "uma execução posterior poderá criar outra sessão."
            )
    if codigo == "PUBLISHED":
        media_id = str(estado.get("id") or "")
        if media_id and media_id != str(container_id):
            estado.pop("reconciliacao_manual", None)
            return registrar_publicado(
                estado,
                media_id,
                persistir,
                confirmacao="media_id_instagram_reconciliado",
            )
        motivo = (
            "O Instagram confirma o container como PUBLISHED, mas não informou "
            "o ID do objeto Reel; a limpeza foi bloqueada para reconciliação manual."
        )
        marcar_reconciliacao_manual(estado, motivo=motivo, persistir=persistir)
        raise RuntimeError(motivo)
    # Esta intenção e o container ficam no remoto antes da chamada irreversível.
    estado.update({"status": "processando", "fase": "publicacao_solicitada"})
    persistir()
    try:
        publicado = graph_post(
            f"{ig_id}/media_publish",
            {"creation_id": container_id, "access_token": token},
        )
    except Exception:
        if reconciliar_instagram_publicado(str(container_id), token):
            media_id = str(estado.get("id") or "")
            if media_id and media_id != str(container_id):
                estado.pop("reconciliacao_manual", None)
                return registrar_publicado(
                    estado,
                    media_id,
                    persistir,
                    confirmacao="media_id_instagram_reconciliado",
                )
            motivo = (
                "O Instagram publicou o container após uma resposta ambígua, mas "
                "o ID do objeto Reel não foi recuperado; a limpeza foi bloqueada."
            )
            marcar_reconciliacao_manual(estado, motivo=motivo, persistir=persistir)
            raise RuntimeError(motivo)
        raise
    if not publicado.get("id"):
        raise RuntimeError(f"Instagram não retornou o Reel publicado: {publicado}")
    return registrar_publicado(
        estado,
        str(publicado["id"]),
        persistir,
        confirmacao="media_publish",
    )


def publicar_facebook(item: dict, estado: dict, persistir) -> str:
    page_id, token = token_pagina()
    caminho: Path | None = None
    try:
        video_id = str(estado.get("video_id") or "")
        upload_url = str(estado.get("upload_url") or "")
        info: dict = {}
        if video_id:
            # Um ID persistido nunca é descartado silenciosamente: consultar e
            # retomar o mesmo upload é o que impede um segundo post.
            info = consultar_video_facebook(video_id, token)
            if video_facebook_publicado(info):
                return registrar_publicado(
                    estado,
                    str(estado.get("id") or video_id),
                    persistir,
                    confirmacao="video_facebook_reconciliado",
                )
            if video_facebook_falhou(info):
                abandonar_identificador(
                    estado,
                    campo_id="video_id",
                    chave_historico="videos_abandonados",
                    identificador=video_id,
                    motivo="Facebook informou falha conclusiva",
                    info_meta=info,
                    persistir=persistir,
                )
                video_id, upload_url, info = "", "", {}
        if not video_id:
            caminho = baixar_midia(item["midia"])
            inicio = graph_post(
                f"{page_id}/video_reels",
                {"upload_phase": "start", "access_token": token},
            )
            video_id, upload_url = str(inicio.get("video_id") or ""), str(
                inicio.get("upload_url") or ""
            )
            if not video_id or not upload_url:
                raise RuntimeError(f"Facebook não iniciou o upload: {inicio}")
            upload_url = validar_upload_url_meta(upload_url, video_id)
            estado.update(
                {
                    "status": "enviando",
                    "fase": "sessao_criada",
                    "video_id": video_id,
                    "upload_url": upload_url,
                }
            )
            persistir()

        if publicacao_facebook_em_andamento(info):
            _aguardar_facebook_seguro(video_id, token, estado, persistir)
            return registrar_publicado(
                estado,
                str(estado.get("id") or video_id),
                persistir,
                confirmacao="video_facebook_processado",
            )

        if not upload_facebook_completo(info):
            if caminho is None:
                caminho = baixar_midia(item["midia"])
            upload_url = validar_upload_url_meta(upload_url, video_id)
            estado.update({"status": "enviando", "fase": "upload_em_andamento"})
            persistir()
            tamanho = caminho.stat().st_size
            with caminho.open("rb") as arquivo:
                try:
                    resposta = requests.post(
                        upload_url,
                        headers={
                            "Authorization": f"OAuth {token}",
                            "offset": "0",
                            "file_size": str(tamanho),
                            "Content-Type": "application/octet-stream",
                        },
                        data=arquivo,
                        timeout=900,
                    )
                except requests.RequestException as erro:
                    raise RuntimeError(
                        "Falha de rede no upload do Reel: "
                        + sanitizar_texto(erro, (token,))
                    ) from None
            if not resposta.ok:
                raise RuntimeError(
                    f"Facebook falhou no upload do Reel: HTTP {resposta.status_code}"
                )
            try:
                corpo_upload = resposta.json()
            except ValueError:
                corpo_upload = {}
            if not isinstance(corpo_upload, dict) or corpo_upload.get("success") is not True:
                raise RuntimeError(
                    f"Facebook não confirmou o upload dos bytes do Reel: {corpo_upload}"
                )
            estado.update({"status": "processando", "fase": "upload_concluido"})
            persistir()

        estado.update({"status": "processando", "fase": "publicacao_solicitada"})
        persistir()
        try:
            fim = graph_post(
                f"{page_id}/video_reels",
                {
                    "upload_phase": "finish",
                    "video_id": video_id,
                    "video_state": "PUBLISHED",
                    "description": estado["legenda"],
                    "access_token": token,
                },
            )
        except Exception:
            reconciliado = consultar_video_facebook(video_id, token)
            if video_facebook_publicado(reconciliado):
                return registrar_publicado(
                    estado,
                    str(estado.get("id") or video_id),
                    persistir,
                    confirmacao="finish_facebook_reconciliado",
                )
            if video_facebook_falhou(reconciliado):
                abandonar_identificador(
                    estado,
                    campo_id="video_id",
                    chave_historico="videos_abandonados",
                    identificador=video_id,
                    motivo="Facebook informou falha conclusiva após finish",
                    info_meta=reconciliado,
                    persistir=persistir,
                )
            raise
        if not fim.get("success"):
            raise RuntimeError(f"Facebook não confirmou o Reel: {fim}")
        estado.update({"status": "processando", "fase": "publicacao_aceita"})
        persistir()
        _aguardar_facebook_seguro(video_id, token, estado, persistir)
        return registrar_publicado(
            estado,
            video_id,
            persistir,
            confirmacao="finish_facebook",
        )
    finally:
        if caminho is not None:
            caminho.unlink(missing_ok=True)


def main() -> None:
    fila = json.loads(FILA_FILE.read_text(encoding="utf-8"))
    validar_fila_operacional(fila, "instagram-facebook-reels")
    conteudos = fila.get("conteudos", [])
    item = alvo_exato(conteudos)
    if not item:
        data = obrigatoria("DATA_PUBLICACAO")
        horario = obrigatoria("HORARIO_PUBLICACAO")
        atrasados = pendencias_anteriores(conteudos, data, horario)
        if atrasados:
            primeiro = min(
                atrasados,
                key=lambda pendente: (pendente["data"], pendente["horario"]),
            )
            raise RuntimeError(
                f"Há {len(atrasados)} Reel(s) vencido(s). "
                f"O primeiro é {primeiro['data']} {primeiro['horario']}."
            )
        print("Nenhum Reel pendente no slot solicitado.")
        return
    if item.get("aprovado") is not True:
        raise RuntimeError("O Reel do slot não possui aprovação explícita.")
    defeito = defeito_do_reel(fila, item)
    if defeito:
        raise SystemExit(f"Reel do slot recusado por defeito: {defeito}")

    def persistir() -> None:
        persistir_fila(FILA_FILE, fila)

    for _ in range(MAX_SUBSTITUICOES_POR_RODADA):
        executar_plataforma(
            item,
            "instagram",
            lambda atual, estado: publicar_instagram(atual, estado, persistir),
            persistir,
        )
        executar_plataforma(
            item,
            "facebook",
            lambda atual, estado: publicar_facebook(atual, estado, persistir),
            persistir,
        )
        if all(item[p].get("status") == "publicado" for p in PLATAFORMAS):
            item.update({"status": "concluido", "concluido_em": datetime.now(BRT).isoformat()})
            persistir()
            return
        if not esgotou_tentativas([item], PLATAFORMAS):
            break
        proximo = pular_e_puxar_proximo(fila.get("conteudos", []), item)
        persistir()
        if proximo is None:
            break
        item = proximo
        if item.get("aprovado") is not True:
            raise RuntimeError("O Reel que assumiu o slot nao possui aprovacao explicita.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
