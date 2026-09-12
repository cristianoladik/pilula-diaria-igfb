"""Publica, em ordem, as partes de um pacote diário de Stories."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import requests

from meta_comum import (
    BRT,
    PLATAFORMAS,
    ROOT,
    aguardar_instagram,
    alvo_exato,
    baixar_midia,
    consultar_container_instagram,
    consultar_video_facebook,
    executar_plataforma,
    graph_get,
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


FILA_FILE = ROOT / "fila" / "fila-stories.json"


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


def _reconciliar_story_facebook(
    page_id: str,
    video_id: str,
    token: str,
    *,
    post_id: str = "",
) -> str | None:
    """Confirma o objeto Story; o status genérico do vídeo não é suficiente."""

    cursor = ""
    cursores_vistos: set[str] = set()
    for _pagina in range(100):
        parametros = {
            "fields": "id,post_id,media_id,status",
            "limit": "100",
            "access_token": token,
        }
        if cursor:
            parametros["after"] = cursor
        resposta = graph_get(f"{page_id}/stories", parametros)
        dados = resposta.get("data")
        if not isinstance(dados, list):
            raise RuntimeError("A Meta não retornou a coleção de Stories da Página.")
        for story in dados:
            if not isinstance(story, dict):
                continue
            status = str(story.get("status") or "").strip().upper()
            if status not in {"PUBLISHED", "ARCHIVED"}:
                continue
            id_story = str(story.get("id") or "")
            post_story = str(story.get("post_id") or "")
            media_story = str(story.get("media_id") or "")
            if post_id and post_id in {id_story, post_story}:
                return post_story or id_story
            if video_id and media_story == video_id:
                return post_story or id_story

        paging = resposta.get("paging")
        cursores = paging.get("cursors") if isinstance(paging, dict) else None
        proximo = str(cursores.get("after") or "") if isinstance(cursores, dict) else ""
        if not proximo:
            return None
        if proximo in cursores_vistos or len(proximo) > 4096:
            raise RuntimeError("A paginação de Stories retornou um cursor inválido/repetido.")
        cursores_vistos.add(proximo)
        cursor = proximo
    raise RuntimeError("A reconciliação excedeu o limite seguro de 10.000 Stories.")


def _abandonar_video_facebook(
    estado: dict, video_id: str, info: dict, persistir, *, motivo: str
) -> None:
    abandonar_identificador(
        estado,
        campo_id="video_id",
        chave_historico="videos_abandonados",
        identificador=video_id,
        motivo=motivo,
        info_meta=info,
        persistir=persistir,
    )


def publicar_instagram(parte: dict, estado: dict, persistir) -> str:
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
        verificar_midia_remota(parte["midia"])
        resposta = graph_post(
            f"{ig_id}/media",
            {
                "media_type": "STORIES",
                "video_url": parte["midia"]["url_publica"],
                "access_token": token,
            },
        )
        container_id = resposta.get("id")
        if not container_id:
            raise RuntimeError(f"Container do Story sem ID: {resposta}")
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
            "o ID do objeto Story; a limpeza foi bloqueada para reconciliação manual."
        )
        marcar_reconciliacao_manual(estado, motivo=motivo, persistir=persistir)
        raise RuntimeError(motivo)
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
                "o ID do objeto Story não foi recuperado; a limpeza foi bloqueada."
            )
            marcar_reconciliacao_manual(estado, motivo=motivo, persistir=persistir)
            raise RuntimeError(motivo)
        raise
    if not publicado.get("id"):
        raise RuntimeError(f"Instagram não retornou o Story publicado: {publicado}")
    return registrar_publicado(
        estado,
        str(publicado["id"]),
        persistir,
        confirmacao="media_publish",
    )


def publicar_facebook(parte: dict, estado: dict, persistir) -> str:
    page_id, token = token_pagina()
    caminho: Path | None = None
    try:
        video_id = str(estado.get("video_id") or "")
        upload_url = str(estado.get("upload_url") or "")
        info: dict = {}
        if video_id:
            info = consultar_video_facebook(video_id, token)
            if (
                video_facebook_publicado(info)
                or publicacao_facebook_em_andamento(info)
                or str(estado.get("fase") or "")
                in {"publicacao_solicitada", "publicacao_aceita"}
            ):
                post_id = _reconciliar_story_facebook(page_id, video_id, token)
                if not post_id:
                    raise RuntimeError(
                        "A sessão de Story pode ter sido publicada, mas o objeto "
                        "não foi comprovado na aresta /stories; nova finalização bloqueada."
                    )
                return registrar_publicado(
                    estado,
                    post_id,
                    persistir,
                    confirmacao="story_facebook_reconciliado",
                )
            if video_facebook_falhou(info):
                _abandonar_video_facebook(
                    estado,
                    video_id,
                    info,
                    persistir,
                    motivo="Facebook informou falha conclusiva",
                )
                video_id, upload_url, info = "", "", {}
        if not video_id:
            caminho = baixar_midia(parte["midia"])
            inicio = graph_post(
                f"{page_id}/video_stories",
                {"upload_phase": "start", "access_token": token},
            )
            video_id, upload_url = str(inicio.get("video_id") or ""), str(
                inicio.get("upload_url") or ""
            )
            if not video_id or not upload_url:
                raise RuntimeError(f"Facebook não iniciou o Story: {inicio}")
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

        if not upload_facebook_completo(info):
            if caminho is None:
                caminho = baixar_midia(parte["midia"])
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
                        "Falha de rede no upload do Story: "
                        + sanitizar_texto(erro, (token,))
                    ) from None
            if not resposta.ok:
                raise RuntimeError(
                    f"Facebook falhou no upload do Story: HTTP {resposta.status_code}"
                )
            try:
                corpo_upload = resposta.json()
            except ValueError:
                corpo_upload = {}
            if not isinstance(corpo_upload, dict) or corpo_upload.get("success") is not True:
                raise RuntimeError(
                    f"Facebook não confirmou o upload dos bytes do Story: {corpo_upload}"
                )
            estado.update({"status": "processando", "fase": "upload_concluido"})
            persistir()

        estado.update(
            {
                "status": "processando",
                "fase": "publicacao_solicitada",
                "finish_solicitado_em": datetime.now(BRT).isoformat(),
            }
        )
        persistir()
        try:
            fim = graph_post(
                f"{page_id}/video_stories",
                {
                    "upload_phase": "finish",
                    "video_id": video_id,
                    "access_token": token,
                },
            )
        except Exception:
            post_id = _reconciliar_story_facebook(page_id, video_id, token)
            if post_id:
                return registrar_publicado(
                    estado,
                    post_id,
                    persistir,
                    confirmacao="story_facebook_finish_reconciliado",
                )
            reconciliado = consultar_video_facebook(video_id, token)
            if video_facebook_falhou(reconciliado) and not video_facebook_publicado(
                reconciliado
            ):
                _abandonar_video_facebook(
                    estado,
                    video_id,
                    reconciliado,
                    persistir,
                    motivo="Facebook informou falha conclusiva após finish",
                )
            raise
        if not fim.get("success") or not fim.get("post_id"):
            raise RuntimeError(f"Facebook não confirmou o Story: {fim}")
        return registrar_publicado(
            estado,
            str(fim["post_id"]),
            persistir,
            confirmacao="finish_facebook",
        )
    finally:
        if caminho is not None:
            caminho.unlink(missing_ok=True)


def main() -> None:
    fila = json.loads(FILA_FILE.read_text(encoding="utf-8"))
    validar_fila_operacional(fila, "instagram-facebook-stories")
    pacote = alvo_exato(fila.get("pacotes", []))
    if not pacote:
        print("Nenhum pacote de Stories pendente no slot solicitado.")
        return
    if pacote.get("aprovado") is not True:
        raise RuntimeError("O pacote de Stories não possui aprovação explícita.")

    def persistir() -> None:
        persistir_fila(FILA_FILE, fila)

    partes = sorted(pacote.get("partes", []), key=lambda parte: parte["ordem"])
    if not partes or [p["ordem"] for p in partes] != list(range(1, len(partes) + 1)):
        raise RuntimeError("O pacote de Stories não tem partes contínuas a partir de 1.")
    for parte in partes:
        executar_plataforma(
            parte,
            "instagram",
            lambda atual, estado: publicar_instagram(atual, estado, persistir),
            persistir,
        )
        executar_plataforma(
            parte,
            "facebook",
            lambda atual, estado: publicar_facebook(atual, estado, persistir),
            persistir,
        )
        if any(parte[p].get("status") == "erro" for p in PLATAFORMAS):
            raise SystemExit(1)
    pacote.update({"status": "concluido", "concluido_em": datetime.now(BRT).isoformat()})
    persistir()


if __name__ == "__main__":
    main()
