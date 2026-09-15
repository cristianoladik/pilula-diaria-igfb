"""Primitivas compartilhadas do publicador Instagram/Facebook.

Este módulo nunca procura um item "mais antigo". Cada execução recebe uma data
e um horário exatos, o que impede uma retentativa atrasada de consumir o slot
seguinte e furar a curva de aquecimento.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


ROOT = Path(__file__).resolve().parent
try:
    BRT = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:  # Python do Windows pode não trazer a base IANA.
    BRT = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
PLATAFORMAS = ("instagram", "facebook")
_DATA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HORA_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_REPOSITORIO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def obrigatoria(nome: str) -> str:
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise RuntimeError(f"Variável obrigatória ausente: {nome}")
    return valor


def sanitizar_texto(texto: object, segredos: Sequence[object] = ()) -> str:
    """Remove tokens conhecidos antes de logs, exceções ou estado versionado."""

    limpo = str(texto)
    valores = [
        os.getenv(nome, "")
        for nome in (
            "IG_ACCESS_TOKEN",
            "FB_PAGE_ACCESS_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        )
    ]
    valores.extend(str(segredo) for segredo in segredos if segredo)
    for segredo in sorted({valor for valor in valores if valor}, key=len, reverse=True):
        limpo = limpo.replace(segredo, "[REDACTED]")
    return re.sub(
        r"(?i)(access_token=)[^&\s'\"<>]+",
        r"\1[REDACTED]",
        limpo,
    )


def graph_base() -> str:
    versao = os.getenv("META_GRAPH_VERSION", "v23.0").strip()
    if not re.fullmatch(r"v\d+\.\d+", versao):
        raise RuntimeError("META_GRAPH_VERSION deve usar o formato vNN.N.")
    return f"https://graph.facebook.com/{versao}"


def _texto_resposta(resposta: requests.Response, segredos: Sequence[object] = ()) -> str:
    return sanitizar_texto(resposta.text[:2000], segredos)


def graph_post(caminho: str, dados: dict, timeout: int = 60) -> dict:
    try:
        resposta = requests.post(
            f"{graph_base()}/{caminho}", data=dados, timeout=timeout
        )
    except requests.RequestException as erro:
        raise RuntimeError(
            "Falha de rede ao chamar a Meta: "
            + sanitizar_texto(erro, (dados.get("access_token"),))
        ) from None
    if not resposta.ok:
        raise RuntimeError(
            f"Meta HTTP {resposta.status_code}: "
            f"{_texto_resposta(resposta, (dados.get('access_token'),))}"
        )
    return resposta.json()


def graph_get(caminho: str, parametros: dict, timeout: int = 30) -> dict:
    try:
        resposta = requests.get(
            f"{graph_base()}/{caminho}", params=parametros, timeout=timeout
        )
    except requests.RequestException as erro:
        raise RuntimeError(
            "Falha de rede ao chamar a Meta: "
            + sanitizar_texto(erro, (parametros.get("access_token"),))
        ) from None
    if not resposta.ok:
        raise RuntimeError(
            f"Meta HTTP {resposta.status_code}: "
            f"{_texto_resposta(resposta, (parametros.get('access_token'),))}"
        )
    return resposta.json()


def salvar_json_atomico(caminho: Path, dados: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(f".{caminho.name}.tmp")
    temporario.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporario.replace(caminho)


def _executar_git(
    argumentos: Sequence[str], *, conferir: bool = True
) -> subprocess.CompletedProcess[str]:
    resultado = subprocess.run(
        ["git", *argumentos],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if conferir and resultado.returncode:
        detalhe = (resultado.stderr or resultado.stdout).strip()[:2000]
        raise RuntimeError(
            f"Git falhou ao tornar o estado durável ({' '.join(argumentos[:2])}): "
            f"{detalhe or resultado.returncode}"
        )
    return resultado


GitExecutor = Callable[..., subprocess.CompletedProcess[str]]
_CHAVES_IDENTIDADE_FILA = (
    "schema_version",
    "projeto",
    "canal",
    "timezone",
    "github",
    "politica",
)
_CHAVES_IMUTAVEIS_MIDIA = {
    "asset",
    "url_publica",
    "sha256",
    "tamanho_bytes",
    "duracao_segundos",
    "remocao_repositorio",
    "remocao_release_tag",
}
_CHAVES_INTENCAO_PLATAFORMA = {
    "container_id",
    "video_id",
    "upload_url",
    "id",
    "legenda",
    "share_to_feed",
}
_CHAVES_HISTORICO_ABANDONO = (
    "containers_abandonados",
    "videos_abandonados",
)
_RANK_STATUS = {
    "pendente": 0,
    "processando": 1,
    "enviando": 1,
    "erro": 2,
    "publicado": 3,
}
_RANK_FASE = {
    "": 0,
    "container_criado": 10,
    "sessao_criada": 10,
    "upload_em_andamento": 20,
    "upload_concluido": 30,
    "publicacao_solicitada": 40,
    "publicacao_aceita": 50,
    "publicado_sem_media_id": 55,
    "identificador_abandonado": 60,
    "publicacao_confirmada": 70,
}


def _objeto(valor: Any, contexto: str) -> dict[str, Any]:
    if not isinstance(valor, Mapping):
        raise RuntimeError(f"Conflito semântico: {contexto} não é um objeto JSON.")
    return dict(valor)


def _mesclar_valores_imutaveis(
    remoto: Mapping[str, Any],
    local: Mapping[str, Any],
    chaves: set[str],
    contexto: str,
) -> None:
    for chave in chaves:
        if chave in remoto and chave in local and remoto[chave] != local[chave]:
            raise RuntimeError(
                f"Conflito semântico em {contexto}.{chave}; "
                "o valor remoto não será sobrescrito."
            )


def _mesclar_midia(
    remoto_bruto: Any, local_bruto: Any, contexto: str
) -> dict[str, Any]:
    remoto = _objeto(remoto_bruto, f"{contexto}.midia remota")
    local = _objeto(local_bruto, f"{contexto}.midia local")
    _mesclar_valores_imutaveis(
        remoto,
        local,
        _CHAVES_IMUTAVEIS_MIDIA,
        f"{contexto}.midia",
    )
    # O remoto é a base. Intenções/marcadores locais apenas completam campos
    # ausentes; um marcador remoto jamais é apagado ou regredido.
    resultado = copy.deepcopy(remoto)
    for chave, valor in local.items():
        if chave not in resultado:
            resultado[chave] = copy.deepcopy(valor)
    return resultado


def _mesclar_historico_abandonados(
    remoto_bruto: Any,
    local_bruto: Any,
    contexto: str,
) -> list[dict[str, Any]]:
    remotos = [] if remoto_bruto is None else remoto_bruto
    locais = [] if local_bruto is None else local_bruto
    if not isinstance(remotos, list) or not isinstance(locais, list):
        raise RuntimeError(f"Conflito semântico: {contexto} precisa ser uma lista.")

    resultado: list[dict[str, Any]] = []
    por_id: dict[str, dict[str, Any]] = {}
    for origem, itens in (("remoto", remotos), ("local", locais)):
        vistos_origem: set[str] = set()
        for indice, item_bruto in enumerate(itens):
            item = _objeto(item_bruto, f"{contexto} {origem} #{indice + 1}")
            identificador = str(item.get("id") or "").strip()
            if not identificador or identificador in vistos_origem:
                raise RuntimeError(
                    f"Conflito semântico: ID ausente/duplicado em {contexto} {origem}."
                )
            vistos_origem.add(identificador)
            existente = por_id.get(identificador)
            if existente is None:
                copia = copy.deepcopy(item)
                resultado.append(copia)
                por_id[identificador] = copia
                continue
            # O registro remoto é autoritativo. O local pode apenas completar
            # metadados que ainda não chegaram ao remoto.
            for chave, valor in item.items():
                if chave not in existente:
                    existente[chave] = copy.deepcopy(valor)
    return resultado


def _normalizar_ids_abandonados(
    estado: Mapping[str, Any],
    containers_abandonados: set[str],
    videos_abandonados: set[str],
) -> dict[str, Any]:
    resultado = copy.deepcopy(dict(estado))
    if str(resultado.get("container_id") or "") in containers_abandonados:
        resultado.pop("container_id", None)
    if str(resultado.get("video_id") or "") in videos_abandonados:
        resultado.pop("video_id", None)
        resultado.pop("upload_url", None)
    if not resultado.get("video_id"):
        resultado.pop("upload_url", None)
    return resultado


def _fase_mais_avancada(
    remoto: Mapping[str, Any],
    local: Mapping[str, Any],
    resultado: Mapping[str, Any],
    contexto: str,
) -> str:
    chave_id = "video_id" if resultado.get("video_id") else "container_id"
    identificador = str(resultado.get(chave_id) or "")
    candidatos: list[tuple[int, int, str]] = []
    for prioridade, estado in ((1, remoto), (0, local)):
        fase = str(estado.get("fase") or "")
        if not fase:
            continue
        if fase not in _RANK_FASE:
            raise RuntimeError(f"Conflito semântico: fase desconhecida em {contexto}.")
        if identificador and str(estado.get(chave_id) or "") != identificador:
            continue
        candidatos.append((_RANK_FASE[fase], prioridade, fase))
    if not candidatos:
        return ""
    return max(candidatos)[2]


def _mesclar_estado_plataforma(
    remoto_bruto: Any,
    local_bruto: Any,
    contexto: str,
) -> dict[str, Any]:
    remoto_original = _objeto(remoto_bruto, f"{contexto} remoto")
    local_original = _objeto(local_bruto, f"{contexto} local")
    historicos = {
        chave: _mesclar_historico_abandonados(
            remoto_original.get(chave),
            local_original.get(chave),
            f"{contexto}.{chave}",
        )
        for chave in _CHAVES_HISTORICO_ABANDONO
    }
    containers_abandonados = {
        str(item["id"]) for item in historicos["containers_abandonados"]
    }
    videos_abandonados = {
        str(item["id"]) for item in historicos["videos_abandonados"]
    }
    remoto = _normalizar_ids_abandonados(
        remoto_original, containers_abandonados, videos_abandonados
    )
    local = _normalizar_ids_abandonados(
        local_original, containers_abandonados, videos_abandonados
    )
    status_remoto = str(remoto.get("status", ""))
    status_local = str(local.get("status", ""))
    if status_remoto not in _RANK_STATUS or status_local not in _RANK_STATUS:
        raise RuntimeError(f"Conflito semântico: status inválido em {contexto}.")
    _mesclar_valores_imutaveis(
        remoto,
        local,
        _CHAVES_INTENCAO_PLATAFORMA,
        contexto,
    )

    # Confirmações e intenções duráveis sempre vencem estados mais antigos. Em
    # empate, o estado remoto é mantido integralmente.
    if _RANK_STATUS[status_local] > _RANK_STATUS[status_remoto]:
        principal, complementar = local, remoto
    else:
        principal, complementar = remoto, local
    resultado = copy.deepcopy(principal)
    status_final = str(principal["status"])
    for chave, valor in complementar.items():
        if chave in {
            "status",
            "fase",
            "finish_solicitado_em",
            *_CHAVES_HISTORICO_ABANDONO,
        }:
            continue
        if status_final == "publicado" and chave in {
            "erro",
            "ultima_tentativa_em",
            "reconciliacao_manual",
        }:
            continue
        if chave not in resultado:
            resultado[chave] = copy.deepcopy(valor)
    resultado["status"] = status_final
    for chave, historico in historicos.items():
        if historico or chave in remoto_original or chave in local_original:
            resultado[chave] = historico

    # Um ID morto em qualquer lado permanece morto. Isso evita que o merge
    # ressuscite uma sessão que a Meta já marcou como ERROR/EXPIRED.
    if str(resultado.get("container_id") or "") in containers_abandonados:
        resultado.pop("container_id", None)
    if str(resultado.get("video_id") or "") in videos_abandonados:
        resultado.pop("video_id", None)
        resultado.pop("upload_url", None)
    if not resultado.get("video_id"):
        resultado.pop("upload_url", None)

    fase = _fase_mais_avancada(remoto, local, resultado, contexto)
    if fase:
        resultado["fase"] = fase
    else:
        resultado.pop("fase", None)
    if status_final == "publicado":
        resultado["fase"] = "publicacao_confirmada"
        resultado.pop("reconciliacao_manual", None)
    marcadores_finish = [
        str(estado["finish_solicitado_em"])
        for estado in (remoto, local)
        if estado.get("finish_solicitado_em")
    ]
    if marcadores_finish:
        resultado["finish_solicitado_em"] = max(marcadores_finish)
    else:
        resultado.pop("finish_solicitado_em", None)
    return resultado


def _mesclar_unidade_publicacao(
    remoto_bruto: Any,
    local_bruto: Any,
    contexto: str,
) -> dict[str, Any]:
    remoto = _objeto(remoto_bruto, f"{contexto} remoto")
    local = _objeto(local_bruto, f"{contexto} local")
    _mesclar_valores_imutaveis(remoto, local, {"ordem"}, contexto)
    resultado = copy.deepcopy(remoto)
    if "midia" in remoto or "midia" in local:
        resultado["midia"] = _mesclar_midia(
            remoto.get("midia"), local.get("midia"), contexto
        )
    for plataforma in PLATAFORMAS:
        resultado[plataforma] = _mesclar_estado_plataforma(
            remoto.get(plataforma),
            local.get(plataforma),
            f"{contexto}.{plataforma}",
        )
    for chave, valor in local.items():
        if chave not in resultado:
            resultado[chave] = copy.deepcopy(valor)
    return resultado


def _mesclar_partes_story(
    remotas_brutas: Any,
    locais_brutas: Any,
    contexto: str,
) -> list[dict[str, Any]]:
    if not isinstance(remotas_brutas, list) or not isinstance(locais_brutas, list):
        raise RuntimeError(f"Conflito semântico: partes inválidas em {contexto}.")

    def por_ordem(partes: list[Any], origem: str) -> dict[int, dict[str, Any]]:
        indice: dict[int, dict[str, Any]] = {}
        for parte_bruta in partes:
            parte = _objeto(parte_bruta, f"{contexto}, parte {origem}")
            ordem = parte.get("ordem")
            if not isinstance(ordem, int) or isinstance(ordem, bool) or ordem in indice:
                raise RuntimeError(
                    f"Conflito semântico: ordem de Story inválida/duplicada em {contexto}."
                )
            indice[ordem] = parte
        return indice

    remotas = por_ordem(remotas_brutas, "remota")
    locais = por_ordem(locais_brutas, "local")
    if set(remotas) != set(locais):
        raise RuntimeError(
            f"Conflito semântico: conjunto de partes divergente em {contexto}; "
            "a fila remota será preservada."
        )
    return [
        _mesclar_unidade_publicacao(
            remotas[ordem], locais[ordem], f"{contexto}, parte {ordem}"
        )
        for ordem in sorted(remotas)
    ]


def _mesclar_item(
    remoto_bruto: Any,
    local_bruto: Any,
    *,
    stories: bool,
) -> dict[str, Any]:
    remoto = _objeto(remoto_bruto, "item remoto")
    local = _objeto(local_bruto, "item local")
    identificador = str(remoto.get("id") or local.get("id") or "<sem-id>")
    contexto = f"item {identificador}"
    _mesclar_valores_imutaveis(
        remoto,
        local,
        {"id", "data", "horario", "aprovado", "origem"},
        contexto,
    )
    if stories:
        resultado = copy.deepcopy(remoto)
        resultado["partes"] = _mesclar_partes_story(
            remoto.get("partes"), local.get("partes"), contexto
        )
    else:
        resultado = _mesclar_unidade_publicacao(remoto, local, contexto)

    status_remoto = str(remoto.get("status", ""))
    status_local = str(local.get("status", ""))
    if status_remoto not in {"pendente", "concluido"} or status_local not in {
        "pendente",
        "concluido",
    }:
        raise RuntimeError(f"Conflito semântico: status de item inválido em {contexto}.")
    if status_remoto == "concluido":
        status_final, fonte_status = "concluido", remoto
    elif status_local == "concluido":
        status_final, fonte_status = "concluido", local
    else:
        status_final, fonte_status = "pendente", remoto
    resultado["status"] = status_final
    if status_final == "concluido" and fonte_status.get("concluido_em") is not None:
        resultado["concluido_em"] = copy.deepcopy(fonte_status["concluido_em"])
    for chave, valor in local.items():
        if chave not in resultado:
            resultado[chave] = copy.deepcopy(valor)
    return resultado


def _identidade_item(item_bruto: Any, contexto: str) -> tuple[str, tuple[str, str]]:
    item = _objeto(item_bruto, contexto)
    identificador = item.get("id")
    data = item.get("data")
    horario = item.get("horario", "09:00")
    if not all(isinstance(valor, str) and valor for valor in (identificador, data, horario)):
        raise RuntimeError(f"Conflito semântico: identidade incompleta em {contexto}.")
    return identificador, (data, horario)


def _indexar_itens(
    itens: list[Any], contexto: str
) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    por_id: dict[str, int] = {}
    por_slot: dict[tuple[str, str], int] = {}
    for indice, item in enumerate(itens):
        identificador, slot = _identidade_item(item, f"{contexto} #{indice + 1}")
        if identificador in por_id or slot in por_slot:
            raise RuntimeError(f"Conflito semântico: ID ou slot duplicado na {contexto}.")
        por_id[identificador] = indice
        por_slot[slot] = indice
    return por_id, por_slot


def mesclar_filas_semantico(
    fila_remota_bruta: Mapping[str, Any],
    fila_local_bruta: Mapping[str, Any],
) -> dict[str, Any]:
    """Une checkpoints por item/slot sem regredir o estado já remoto."""

    remoto = _objeto(fila_remota_bruta, "fila remota")
    local = _objeto(fila_local_bruta, "fila local")
    for chave in _CHAVES_IDENTIDADE_FILA:
        if chave in remoto and chave in local and remoto[chave] != local[chave]:
            raise RuntimeError(
                f"Conflito semântico em fila.{chave}; a fila remota será preservada."
            )

    canal = str(remoto.get("canal") or local.get("canal") or "")
    if canal == "instagram-facebook-reels":
        colecao, stories = "conteudos", False
    elif canal == "instagram-facebook-stories":
        colecao, stories = "pacotes", True
    else:
        raise RuntimeError("Conflito semântico: canal de fila desconhecido.")
    remotos = remoto.get(colecao)
    locais = local.get(colecao)
    if not isinstance(remotos, list) or not isinstance(locais, list):
        raise RuntimeError(f"Conflito semântico: fila.{colecao} deve ser uma lista.")
    por_id_remoto, por_slot_remoto = _indexar_itens(remotos, "fila remota")
    _indexar_itens(locais, "fila local")

    resultado_itens = copy.deepcopy(remotos)
    for item_local in locais:
        identificador, slot = _identidade_item(item_local, "item local")
        correspondencias = {
            indice
            for indice in (por_id_remoto.get(identificador), por_slot_remoto.get(slot))
            if indice is not None
        }
        if len(correspondencias) > 1:
            raise RuntimeError(
                f"Conflito semântico: ID {identificador!r} e slot {slot!r} "
                "apontam para itens remotos diferentes."
            )
        if not correspondencias:
            resultado_itens.append(copy.deepcopy(item_local))
            continue
        indice = correspondencias.pop()
        id_remoto, slot_remoto = _identidade_item(resultado_itens[indice], "item remoto")
        if id_remoto != identificador or slot_remoto != slot:
            raise RuntimeError(
                f"Conflito semântico no ID/slot {identificador!r}; "
                "a fila remota será preservada."
            )
        resultado_itens[indice] = _mesclar_item(
            resultado_itens[indice], item_local, stories=stories
        )

    resultado_itens.sort(
        key=lambda item: (
            str(item.get("data", "")),
            str(item.get("horario", "09:00")),
            str(item.get("id", "")),
        )
    )
    resultado = copy.deepcopy(remoto)
    resultado[colecao] = resultado_itens
    for chave, valor in local.items():
        if chave not in resultado:
            resultado[chave] = copy.deepcopy(valor)
    return resultado


def _detalhe_git(resultado: subprocess.CompletedProcess[str]) -> str:
    return (resultado.stderr or resultado.stdout or "").strip()[:2000]


def _non_fast_forward(resultado: subprocess.CompletedProcess[str]) -> bool:
    texto = f"{resultado.stdout or ''}\n{resultado.stderr or ''}"
    return bool(
        re.search(
            r"non-fast-forward|fetch first|\[rejected\]",
            texto,
            flags=re.IGNORECASE,
        )
    )


def _caminhos_oficiais(caminhos: Sequence[Path]) -> tuple[tuple[Path, str], ...]:
    pasta_filas = (ROOT / "fila").resolve()
    resultado: list[tuple[Path, str]] = []
    vistos: set[str] = set()
    for caminho_bruto in caminhos:
        caminho = Path(caminho_bruto).resolve()
        if caminho.parent != pasta_filas:
            raise RuntimeError("Somente filas oficiais podem ser persistidas pelo publicador.")
        relativo = caminho.relative_to(ROOT.resolve()).as_posix()
        if relativo not in vistos:
            resultado.append((caminho, relativo))
            vistos.add(relativo)
    if not resultado:
        raise RuntimeError("Informe ao menos uma fila oficial para persistir.")
    return tuple(resultado)


def _carregar_fila_local(caminho: Path) -> dict[str, Any]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as erro:
        raise RuntimeError(f"Fila local inválida: {caminho}") from erro
    if not isinstance(dados, dict):
        raise RuntimeError(f"Fila local precisa ser um objeto JSON: {caminho}")
    return dados


def _identidade_na_lista(chave: str, valor: Any) -> tuple[Any, ...] | None:
    if not isinstance(valor, Mapping):
        return None
    if chave in {"conteudos", "pacotes"}:
        identificador = valor.get("id")
        data = valor.get("data")
        horario = valor.get("horario", "09:00")
        if all(
            isinstance(parte, str) and bool(parte)
            for parte in (identificador, data, horario)
        ):
            return ("item", identificador, data, horario)
    elif chave == "partes":
        ordem = valor.get("ordem")
        if isinstance(ordem, int) and not isinstance(ordem, bool):
            return ("parte", ordem)
    elif chave in _CHAVES_HISTORICO_ABANDONO:
        identificador = valor.get("id")
        if isinstance(identificador, str) and identificador:
            return ("historico", identificador)
    return None


def _atualizar_lista_inplace(
    destino: list[Any],
    autoritativa: Sequence[Any],
    *,
    chave: str,
) -> None:
    identidades = [_identidade_na_lista(chave, valor) for valor in destino]
    usa_identidade = chave in {
        "conteudos",
        "pacotes",
        "partes",
        *_CHAVES_HISTORICO_ABANDONO,
    }
    disponiveis: dict[tuple[Any, ...], Any] = {}
    if usa_identidade:
        for identidade, valor in zip(identidades, destino):
            if identidade is not None and identidade not in disponiveis:
                disponiveis[identidade] = valor

    atualizados: list[Any] = []
    for indice, valor_autoritativo in enumerate(autoritativa):
        identidade = _identidade_na_lista(chave, valor_autoritativo)
        if usa_identidade and identidade is not None:
            valor_atual = disponiveis.pop(identidade, None)
        elif not usa_identidade and indice < len(destino):
            valor_atual = destino[indice]
        else:
            valor_atual = None

        if isinstance(valor_atual, dict) and isinstance(
            valor_autoritativo, Mapping
        ):
            _atualizar_mapeamento_inplace(valor_atual, valor_autoritativo)
            atualizados.append(valor_atual)
        elif isinstance(valor_atual, list) and isinstance(
            valor_autoritativo, Sequence
        ) and not isinstance(valor_autoritativo, (str, bytes, bytearray)):
            _atualizar_lista_inplace(
                valor_atual, valor_autoritativo, chave=chave
            )
            atualizados.append(valor_atual)
        else:
            atualizados.append(copy.deepcopy(valor_autoritativo))
    destino[:] = atualizados


def _atualizar_mapeamento_inplace(
    destino: dict[str, Any], autoritativa: Mapping[str, Any]
) -> None:
    """Adota a árvore autoritativa sem invalidar referências do publicador."""

    for chave in tuple(destino):
        if chave not in autoritativa:
            del destino[chave]
    for chave, valor_autoritativo in autoritativa.items():
        valor_atual = destino.get(chave)
        if isinstance(valor_atual, dict) and isinstance(
            valor_autoritativo, Mapping
        ):
            _atualizar_mapeamento_inplace(valor_atual, valor_autoritativo)
        elif isinstance(valor_atual, list) and isinstance(
            valor_autoritativo, Sequence
        ) and not isinstance(valor_autoritativo, (str, bytes, bytearray)):
            _atualizar_lista_inplace(
                valor_atual, valor_autoritativo, chave=chave
            )
        elif valor_atual != valor_autoritativo:
            destino[chave] = copy.deepcopy(valor_autoritativo)


def _json_git(
    referencia: str,
    relativo: str,
    *,
    executor: GitExecutor,
) -> dict[str, Any]:
    resposta = executor(["show", f"{referencia}:{relativo}"], conferir=False)
    if resposta.returncode:
        raise RuntimeError(
            f"Git não encontrou {relativo} no estado remoto: {_detalhe_git(resposta)}"
        )
    try:
        dados = json.loads(resposta.stdout)
    except json.JSONDecodeError as erro:
        raise RuntimeError(f"Git devolveu JSON remoto inválido para {relativo}.") from erro
    if not isinstance(dados, dict):
        raise RuntimeError(f"A fila remota {relativo} não é um objeto JSON.")
    return dados


def _validar_mescla_semantica(
    mescladas: Mapping[str, Mapping[str, Any]],
    *,
    executor: GitExecutor,
) -> None:
    relativos = ("fila/fila-reels.json", "fila/fila-stories.json")
    filas = {
        relativo: (
            dict(mescladas[relativo])
            if relativo in mescladas
            else _json_git("FETCH_HEAD", relativo, executor=executor)
        )
        for relativo in relativos
    }
    # Import local evita acoplar a publicação normal ao CLI do validador.
    # validar_filas() devolve só o que derruba a fila inteira. Defeito de um item
    # ficou de fora de propósito: senão um vídeo errado agendado para daqui a dois
    # meses impediria de salvar a publicação que acabou de dar certo (12/09/2026).
    from validar_filas import validar_filas

    erros = validar_filas(
        filas["fila/fila-reels.json"],
        filas["fila/fila-stories.json"],
    )
    if erros:
        resumo = "; ".join(erros[:10])
        if len(erros) > 10:
            resumo += f"; ... ({len(erros) - 10} erro(s) adicional(is))"
        raise RuntimeError(
            "A reconciliação semântica produziria filas inválidas; "
            f"nenhum push foi feito: {resumo}"
        )


def _abortar_merge(executor: GitExecutor) -> None:
    executor(["merge", "--abort"], conferir=False)


def _reconciliar_com_remoto(
    arquivos: tuple[tuple[Path, str], ...],
    intencoes: Mapping[str, Mapping[str, Any]],
    mensagem: str,
    *,
    executor: GitExecutor,
) -> None:
    branch_resultado = executor(["rev-parse", "--abbrev-ref", "HEAD"], conferir=False)
    branch = (branch_resultado.stdout or "").strip()
    if branch_resultado.returncode or not branch or branch == "HEAD":
        raise RuntimeError("Git não conseguiu identificar a branch para reconciliar a fila.")
    executor(["fetch", "--no-tags", "origin", branch])

    mescladas: dict[str, dict[str, Any]] = {}
    for _, relativo in arquivos:
        remota = _json_git("FETCH_HEAD", relativo, executor=executor)
        mescladas[relativo] = mesclar_filas_semantico(remota, intencoes[relativo])
    _validar_mescla_semantica(mescladas, executor=executor)

    merge = executor(["merge", "--no-commit", "--no-ff", "FETCH_HEAD"], conferir=False)
    merge_ativo = True
    try:
        nao_mesclados = executor(
            ["diff", "--name-only", "--diff-filter=U"], conferir=False
        )
        if nao_mesclados.returncode:
            raise RuntimeError("Git não conseguiu listar conflitos da reconciliação.")
        conflitos = {
            linha.strip().replace("\\", "/")
            for linha in (nao_mesclados.stdout or "").splitlines()
            if linha.strip()
        }
        permitidos = {relativo for _, relativo in arquivos}
        inesperados = sorted(conflitos - permitidos)
        if inesperados:
            raise RuntimeError(
                "Git encontrou conflito fora das filas autorizadas: "
                + ", ".join(inesperados)
            )
        if merge.returncode not in {0, 1} or (merge.returncode == 1 and not conflitos):
            raise RuntimeError(
                "Git não conseguiu iniciar a reconciliação semântica: "
                + (_detalhe_git(merge) or str(merge.returncode))
            )

        for caminho, relativo in arquivos:
            salvar_json_atomico(caminho, mescladas[relativo])
        executor(["add", "--", *(relativo for _, relativo in arquivos)])
        restantes = executor(
            ["diff", "--name-only", "--diff-filter=U"], conferir=False
        )
        if restantes.returncode or (restantes.stdout or "").strip():
            raise RuntimeError("A reconciliação deixou conflitos Git não resolvidos.")
        executor(
            [
                "-c",
                "user.name=GitHub Actions",
                "-c",
                "user.email=actions@github.com",
                "commit",
                "-m",
                f"{mensagem} (reconciliação semântica)",
            ]
        )
        merge_ativo = False
    except Exception:
        if merge_ativo:
            _abortar_merge(executor)
        raise


def persistir_arquivos_git(
    caminhos: Sequence[Path],
    mensagem: str,
    *,
    max_tentativas: int = 3,
    executor: GitExecutor | None = None,
) -> dict[str, dict[str, Any]]:
    """Commita e envia filas com retry limitado e merge semântico por item."""

    executor = executor or _executar_git
    if (
        isinstance(max_tentativas, bool)
        or not isinstance(max_tentativas, int)
        or max_tentativas < 1
    ):
        raise ValueError("max_tentativas deve ser um inteiro positivo.")
    arquivos = _caminhos_oficiais(caminhos)
    intencoes: dict[str, Mapping[str, Any]] = {}
    for caminho, relativo in arquivos:
        intencoes[relativo] = copy.deepcopy(_carregar_fila_local(caminho))

    relativos = [relativo for _, relativo in arquivos]
    executor(["add", "--", *relativos])
    diferenca = executor(
        ["diff", "--cached", "--quiet", "--", *relativos], conferir=False
    )
    if diferenca.returncode not in {0, 1}:
        raise RuntimeError("Git não conseguiu comparar o estado preparado das filas.")
    if diferenca.returncode == 1:
        executor(
            [
                "-c",
                "user.name=GitHub Actions",
                "-c",
                "user.email=actions@github.com",
                "commit",
                "-m",
                mensagem,
                "--",
                *relativos,
            ]
        )

    for tentativa in range(1, max_tentativas + 1):
        push = executor(["push"], conferir=False)
        if push.returncode == 0:
            return {
                relativo: _carregar_fila_local(caminho)
                for caminho, relativo in arquivos
            }
        if not _non_fast_forward(push):
            raise RuntimeError(
                "Git push falhou sem condição segura de retry: "
                + (_detalhe_git(push) or str(push.returncode))
            )
        if tentativa == max_tentativas:
            raise RuntimeError(
                f"Git push continuou concorrente após {max_tentativas} tentativas; "
                "nenhum estado remoto foi sobrescrito."
            )
        _reconciliar_com_remoto(
            arquivos,
            intencoes,
            mensagem,
            executor=executor,
        )


def persistir_fila(caminho: Path, dados: dict) -> None:
    """Persiste a fila e, no Actions, confirma cada transição no remoto.

    O identificador da operação é enviado ao GitHub *antes* das chamadas de
    publicação. Assim, se o runner cair depois de uma chamada irreversível, a
    retentativa reutiliza o mesmo container/video em vez de criar outro post.
    """

    caminho = caminho.resolve()
    pasta_filas = (ROOT / "fila").resolve()
    if caminho.parent != pasta_filas:
        raise RuntimeError("Somente uma fila oficial pode ser persistida pelo publicador.")
    salvar_json_atomico(caminho, dados)
    if os.getenv("PERSISTIR_ESTADO_REMOTO", "false").strip().lower() != "true":
        return

    slot = " ".join(
        parte
        for parte in (
            os.getenv("DATA_PUBLICACAO", "").strip(),
            os.getenv("HORARIO_PUBLICACAO", "").strip(),
        )
        if parte
    )
    mensagem = f"chore: persistir operação Meta {slot}".strip()
    relativo = caminho.relative_to(ROOT).as_posix()
    autoritativas = persistir_arquivos_git([caminho], mensagem)
    _atualizar_mapeamento_inplace(dados, autoritativas[relativo])


STATUS_FINAIS = {"concluido", "pulado"}
# Depois de tantas tentativas falhas numa rede, o item sai da vez e o proximo
# pendente da fila assume o horario. Regra do Cristiano em 15/09/2026: quando
# ha recusa, tentar os proximos videos da fila ate conseguir.
MAX_TENTATIVAS_ANTES_DE_PULAR = 3
MAX_SUBSTITUICOES_POR_RODADA = 5


def esgotou_tentativas(blocos: list[dict], plataformas) -> bool:
    for bloco in blocos:
        for plataforma in plataformas:
            estado = bloco.get(plataforma, {})
            if estado.get("status") == "erro" and int(estado.get("tentativas", 0)) >= MAX_TENTATIVAS_ANTES_DE_PULAR:
                return True
    return False


def pular_e_puxar_proximo(
    colecao: list[dict], item: dict, *, campo_horario: str = "horario"
) -> dict | None:
    """Marca o item como pulado e traz o proximo pendente para o mesmo horario."""

    data = str(item.get("data", ""))
    horario = str(item.get(campo_horario, "09:00"))
    item["status"] = "pulado"
    item["pulado_em"] = datetime.now(BRT).isoformat()
    candidatos = sorted(
        (
            outro
            for outro in colecao
            if outro is not item
            and outro.get("status") == "pendente"
            and (str(outro.get("data", "")), str(outro.get(campo_horario, "09:00"))) > (data, horario)
        ),
        key=lambda outro: (str(outro.get("data", "")), str(outro.get(campo_horario, "09:00"))),
    )
    if not candidatos:
        print(f"PULADO {item.get('id')}: sem proximo pendente na fila para assumir {data} {horario}.")
        return None
    proximo = candidatos[0]
    proximo["reagendado_de"] = f"{proximo.get('data')} {proximo.get(campo_horario, '09:00')}"
    proximo["data"] = data
    proximo[campo_horario] = horario
    print(f"PULADO {item.get('id')} apos {MAX_TENTATIVAS_ANTES_DE_PULAR} tentativas; {proximo.get('id')} assume {data} {horario}.")
    return proximo


def alvo_exato(colecao: list[dict], *, campo_horario: str = "horario") -> dict | None:
    data = obrigatoria("DATA_PUBLICACAO")
    horario = obrigatoria("HORARIO_PUBLICACAO")
    if not _DATA_RE.fullmatch(data):
        raise RuntimeError("DATA_PUBLICACAO deve usar AAAA-MM-DD.")
    if not _HORA_RE.fullmatch(horario):
        raise RuntimeError("HORARIO_PUBLICACAO deve usar HH:MM.")
    encontrados = [
        item
        for item in colecao
        if item.get("data") == data
        and item.get(campo_horario, "09:00") == horario
        and item.get("status") not in STATUS_FINAIS
    ]
    if len(encontrados) > 1:
        raise RuntimeError(f"Mais de um item pendente no slot {data} {horario}.")
    return encontrados[0] if encontrados else None


def validar_fila_operacional(fila: Mapping[str, object], canal: str) -> None:
    """Recusa uso direto de uma fila pertencente a outro destino."""

    if fila.get("schema_version") != 1:
        raise RuntimeError("A fila não usa o schema_version aprovado.")
    if (
        fila.get("projeto") != "Pílula Diária"
        or fila.get("canal") != canal
        or fila.get("timezone") != "America/Sao_Paulo"
    ):
        raise RuntimeError("A fila pertence a outro projeto, canal ou fuso horário.")
    github = fila.get("github")
    if not isinstance(github, Mapping):
        raise RuntimeError("A fila não declara sua origem GitHub.")
    if (
        github.get("repositorio") != os.getenv("GITHUB_REPOSITORY", "").strip()
        or github.get("release_tag")
        != os.getenv("RELEASE_TAG", "fila-instagram-facebook").strip()
    ):
        raise RuntimeError("A fila não corresponde ao repositório/Release desta execução.")


def consultar_container_instagram(container_id: str, token: str) -> dict:
    return graph_get(
        container_id,
        {"fields": "status_code,status", "access_token": token},
    )


def aguardar_instagram(container_id: str, token: str) -> str:
    for tentativa in range(36):
        estado = consultar_container_instagram(container_id, token)
        codigo = estado.get("status_code", "")
        print(f"Instagram [{tentativa + 1}/36]: {codigo}")
        if codigo == "FINISHED":
            return codigo
        if codigo == "PUBLISHED":
            return codigo
        if codigo in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"A Meta recusou o processamento: {estado}")
        time.sleep(10)
    raise TimeoutError("O Instagram demorou mais de seis minutos para processar a mídia.")


def reconciliar_instagram_publicado(container_id: str, token: str) -> bool:
    """Confirma eventual sucesso após resposta ambígua sem reenviar outra mídia."""

    for tentativa in range(6):
        estado = consultar_container_instagram(container_id, token)
        codigo = str(estado.get("status_code", ""))
        if codigo == "PUBLISHED":
            return True
        if codigo in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"A Meta recusou o container após a publicação: {estado}")
        if tentativa < 5:
            time.sleep(10)
    return False


def registrar_publicado(
    estado: dict,
    media_id: str,
    persistir: Callable[[], None],
    *,
    confirmacao: str,
) -> str:
    estado.update(
        {
            "status": "publicado",
            "fase": "publicacao_confirmada",
            "id": str(media_id),
            "publicado_em": datetime.now(BRT).isoformat(),
            "confirmacao": confirmacao,
        }
    )
    estado.pop("erro", None)
    estado.pop("ultima_tentativa_em", None)
    estado.pop("ultima_falha_de_persistencia", None)
    estado.pop("reconciliacao_manual", None)
    persistir()
    return str(media_id)


def validar_url_midia(midia: Mapping[str, object]) -> None:
    """Amarra a URL ao repositório/tag/asset desta execução."""

    url = str(midia.get("url_publica", "")).strip()
    asset = str(midia.get("asset", "")).strip()
    partes = urlsplit(url)
    if partes.scheme != "https" or (partes.hostname or "").casefold() != "github.com":
        raise RuntimeError("A mídia precisa vir de uma Release HTTPS do GitHub.")
    if partes.username or partes.password or partes.port or partes.query or partes.fragment:
        raise RuntimeError("A URL pública da mídia contém componentes não permitidos.")
    repositorio = os.getenv("GITHUB_REPOSITORY", "").strip()
    tag = os.getenv("RELEASE_TAG", "fila-instagram-facebook").strip()
    if not _REPOSITORIO_RE.fullmatch(repositorio) or not tag:
        raise RuntimeError("GITHUB_REPOSITORY/RELEASE_TAG não identificam a fila desta execução.")
    esperado = [*repositorio.split("/"), "releases", "download", tag, asset]
    recebido = [unquote(parte) for parte in partes.path.strip("/").split("/")]
    if recebido != esperado or not asset or Path(asset).name != asset:
        raise RuntimeError("A URL não corresponde ao repositório, tag e asset declarados na fila.")


def verificar_midia_remota(midia: dict) -> None:
    caminho = baixar_midia(midia)
    caminho.unlink(missing_ok=True)


def validar_upload_url_meta(url: str, video_id: str) -> str:
    partes = urlsplit(str(url).strip())
    host = (partes.hostname or "").casefold()
    try:
        porta = partes.port
    except ValueError as erro:
        raise RuntimeError("A Meta retornou uma URL de upload inesperada.") from erro
    segmentos = [unquote(parte) for parte in partes.path.split("/")]
    if (
        partes.scheme != "https"
        or host != "rupload.facebook.com"
        or partes.username
        or partes.password
        or partes.query
        or partes.fragment
        or porta not in {None, 443}
        or len(segmentos) != 4
        or segmentos[0] != ""
        or segmentos[1] != "video-upload"
        or re.fullmatch(r"v\d+\.\d+", segmentos[2]) is None
        or segmentos[3] != str(video_id)
    ):
        raise RuntimeError("A Meta retornou uma URL de upload inesperada.")
    return partes.geturl()


def consultar_video_facebook(video_id: str, token: str) -> dict:
    return graph_get(
        str(video_id),
        {"fields": "status,published", "access_token": token},
    )


def _fase_facebook(info: Mapping[str, object], nome: str) -> str:
    status = info.get("status")
    if not isinstance(status, Mapping):
        return ""
    fase = status.get(nome)
    if not isinstance(fase, Mapping):
        return ""
    return str(fase.get("status", "")).strip().casefold()


def video_facebook_publicado(info: Mapping[str, object]) -> bool:
    if info.get("published") is True:
        return True
    return _fase_facebook(info, "publishing_phase") in {"complete", "completed"}


def upload_facebook_completo(info: Mapping[str, object]) -> bool:
    return _fase_facebook(info, "uploading_phase") in {"complete", "completed"}


def publicacao_facebook_em_andamento(info: Mapping[str, object]) -> bool:
    return _fase_facebook(info, "publishing_phase") in {
        "started",
        "processing",
        "in_progress",
        "complete",
        "completed",
    }


def aguardar_facebook_publicado(video_id: str, token: str) -> None:
    for tentativa in range(36):
        info = consultar_video_facebook(video_id, token)
        if video_facebook_publicado(info):
            return
        status = info.get("status")
        video_status = ""
        if isinstance(status, Mapping):
            video_status = str(status.get("video_status", "")).casefold()
        if video_status in {"error", "failed", "expired"}:
            raise RuntimeError(f"A Meta recusou o vídeo do Facebook: {info}")
        print(f"Facebook [{tentativa + 1}/36]: {video_status or 'aguardando'}")
        time.sleep(10)
    raise TimeoutError("O Facebook não confirmou a publicação em seis minutos.")


def baixar_midia(midia: dict) -> Path:
    validar_url_midia(midia)
    url = str(midia.get("url_publica", "")).strip()
    nome = str(midia.get("asset", "")).strip()
    if not url.startswith("https://") or not nome or Path(nome).name != nome:
        raise RuntimeError("A fila não contém uma URL HTTPS e um nome de asset válidos.")
    cache = Path(obrigatoria("MEDIA_CACHE_DIR"))
    destino = cache / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tamanho = 0
    try:
        with requests.get(url, stream=True, timeout=(30, 900)) as resposta:
            if not resposta.ok:
                raise RuntimeError(
                    f"Não foi possível baixar {nome}: HTTP {resposta.status_code}"
                )
            with destino.open("wb") as arquivo:
                for bloco in resposta.iter_content(chunk_size=1024 * 1024):
                    if bloco:
                        arquivo.write(bloco)
                        digest.update(bloco)
                        tamanho += len(bloco)
        hash_esperado = str(midia.get("sha256", "")).lower()
        tamanho_esperado = int(midia.get("tamanho_bytes", 0))
        if not hash_esperado or digest.hexdigest().lower() != hash_esperado:
            raise RuntimeError(f"SHA-256 divergente para {nome}.")
        if tamanho_esperado <= 0 or tamanho != tamanho_esperado:
            raise RuntimeError(f"Tamanho divergente para {nome}.")
        return destino
    except Exception:
        destino.unlink(missing_ok=True)
        raise


def token_pagina() -> tuple[str, str]:
    token_sistema = obrigatoria("FB_PAGE_ACCESS_TOKEN")
    page_id = obrigatoria("FB_PAGE_ID")
    token = graph_get(
        page_id,
        {"fields": "access_token", "access_token": token_sistema},
    ).get("access_token")
    if not token:
        raise RuntimeError("A Meta não retornou o token final da Página.")
    return page_id, str(token)


def executar_plataforma(
    item: dict,
    plataforma: str,
    publicar: Callable[[dict, dict], str],
    persistir: Callable[[], None],
) -> None:
    estado = item[plataforma]
    if estado.get("status") == "publicado":
        return
    estado["tentativas"] = int(estado.get("tentativas", 0)) + 1
    try:
        media_id = publicar(item, estado)
        if estado.get("status") != "publicado":
            estado.update(
                {
                    "status": "publicado",
                    "fase": "publicacao_confirmada",
                    "id": str(media_id),
                    "publicado_em": datetime.now(BRT).isoformat(),
                }
            )
        estado.pop("erro", None)
        estado.pop("ultima_tentativa_em", None)
        estado.pop("ultima_falha_de_persistencia", None)
        estado.pop("reconciliacao_manual", None)
    except Exception as erro:
        mensagem_erro = sanitizar_texto(erro)
        if estado.get("status") == "publicado":
            estado["ultima_falha_de_persistencia"] = mensagem_erro
        else:
            estado.update(
                {
                    "status": "erro",
                    "erro": mensagem_erro,
                    "ultima_tentativa_em": datetime.now(BRT).isoformat(),
                }
            )
        print(f"ERRO {plataforma}: {mensagem_erro}")
    finally:
        persistir()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Persistência durável das filas de publicação."
    )
    subcomandos = parser.add_subparsers(dest="comando", required=True)
    persistir = subcomandos.add_parser(
        "persistir-git",
        help="commita e envia filas, reconciliando concorrência por item/slot",
    )
    persistir.add_argument(
        "--fila",
        action="append",
        required=True,
        type=Path,
        help="caminho de uma fila oficial (repita para incluir outra fila)",
    )
    persistir.add_argument("--mensagem", required=True, help="mensagem do commit")
    argumentos = parser.parse_args(argv)

    if argumentos.comando == "persistir-git":
        mensagem = argumentos.mensagem.strip()
        if not mensagem:
            parser.error("--mensagem não pode ser vazia")
        caminhos = [
            caminho if caminho.is_absolute() else ROOT / caminho
            for caminho in argumentos.fila
        ]
        persistir_arquivos_git(caminhos, mensagem)
        return 0
    parser.error("subcomando desconhecido")


if __name__ == "__main__":
    raise SystemExit(main())
