"""Validação offline e autossuficiente das filas do Pílula Diária.

As regras operacionais ficam versionadas dentro das próprias filas. Este módulo
não lê configuração externa e não chama GitHub, Meta ou qualquer outro serviço.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit


SCHEMA_VERSION = 1
PROJETO = "Pílula Diária"
CANAL_REELS = "instagram-facebook-reels"
CANAL_STORIES = "instagram-facebook-stories"
TIMEZONE = "America/Sao_Paulo"
PLACEHOLDER = "__CONFIGURAR__"
# A ordem desta lista e a ordem em que os horarios entram na escada, NAO a
# ordem do relogio. A semana 1 usa o primeiro, a semana 2 os dois primeiros, e
# assim por diante. Por isso ela comeca pelos melhores horarios do dia no
# Brasil: 19h, 12h e 21h. Escolha do Cristiano em 12/09/2026, para a Pilula
# Diaria subir de 1 ate 10 Reels por dia, um a mais por semana.
HORARIOS_REELS = (
    "19:00",
    "12:00",
    "21:00",
    "08:00",
    "16:00",
    "06:00",
    "18:00",
    "10:00",
    "22:00",
    "14:00",
)
RAMPA_REELS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
HORARIO_STORY = "09:00"
LIMITE_STORY = Decimal("59")
MINIMO_REEL = Decimal("4")
MAXIMO_REEL = Decimal("180")  # 3 minutos, regra do Cristiano em 10/09/2026
PREFIXO_BLOQUEADO = "932 -"

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORIO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A legenda repete "Siga @arroba" em ate quatro linhas, para ficar bem visivel
# no aplicativo. Pedido do Cristiano em 12/09/2026. Continua entrando so isso:
# nenhum outro texto e aceito.
_LINHA_SIGA = r"Siga @[A-Za-z0-9._]{1,30}"
LEGENDA_RE = re.compile(r"^" + _LINHA_SIGA + r"(?:\n" + _LINHA_SIGA + r"){0,3}$")
ESTADOS_INSTAGRAM = {"pendente", "processando", "publicado", "erro"}
ESTADOS_FACEBOOK = {"pendente", "enviando", "processando", "publicado", "erro"}
STATUS_ITENS = {"pendente", "concluido"}
FASES_INSTAGRAM = {
    "container_criado",
    "publicacao_solicitada",
    "publicado_sem_media_id",
    "identificador_abandonado",
    "publicacao_confirmada",
}
FASES_FACEBOOK = {
    "sessao_criada",
    "upload_em_andamento",
    "upload_concluido",
    "publicacao_solicitada",
    "publicacao_aceita",
    "identificador_abandonado",
    "publicacao_confirmada",
}


def _upload_url_facebook_valida(valor: Any, video_id: str) -> bool:
    if not isinstance(valor, str) or not valor or not video_id:
        return False
    partes = urlsplit(valor)
    try:
        porta = partes.port
    except ValueError:
        return False
    segmentos = [unquote(parte) for parte in partes.path.split("/")]
    return bool(
        partes.scheme == "https"
        and (partes.hostname or "").casefold() == "rupload.facebook.com"
        and not partes.username
        and not partes.password
        and porta in {None, 443}
        and not partes.query
        and not partes.fragment
        and len(segmentos) == 4
        and segmentos[0] == ""
        and segmentos[1] == "video-upload"
        and re.fullmatch(r"v\d+\.\d+", segmentos[2])
        and segmentos[3] == video_id
    )


@dataclass(frozen=True, slots=True)
class PoliticaValidada:
    repositorio: str | None
    release_tag: str | None
    d0: date | None
    legenda_reels: str | None
    limite_story: Decimal


@dataclass(slots=True)
class Unicidade:
    ids: set[str] = field(default_factory=set)
    assets: set[str] = field(default_factory=set)
    shas_midias: set[str] = field(default_factory=set)
    shas_origens: set[str] = field(default_factory=set)


def erro(condicao: bool, mensagem: str, erros: list[str]) -> None:
    if not condicao:
        erros.append(mensagem)


def _mapa(valor: Any, contexto: str, erros: list[str]) -> Mapping[str, Any]:
    if not isinstance(valor, Mapping):
        erros.append(f"{contexto}: deve ser um objeto JSON")
        return {}
    return valor


def _lista(valor: Any, contexto: str, erros: list[str]) -> list[Any]:
    if not isinstance(valor, list):
        erros.append(f"{contexto}: deve ser uma lista")
        return []
    return valor


def _decimal(valor: Any, contexto: str, erros: list[str]) -> Decimal | None:
    if isinstance(valor, bool):
        erros.append(f"{contexto}: deve ser um número finito")
        return None
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        erros.append(f"{contexto}: deve ser um número finito")
        return None
    if not numero.is_finite():
        erros.append(f"{contexto}: deve ser um número finito")
        return None
    return numero


def _data_iso(valor: Any, contexto: str, erros: list[str]) -> date | None:
    if not isinstance(valor, str):
        erros.append(f"{contexto}: deve usar AAAA-MM-DD")
        return None
    try:
        resultado = date.fromisoformat(valor)
    except ValueError:
        erros.append(f"{contexto}: deve usar AAAA-MM-DD")
        return None
    if resultado.isoformat() != valor:
        erros.append(f"{contexto}: deve usar AAAA-MM-DD")
        return None
    return resultado


def _timestamp(valor: Any, contexto: str, erros: list[str]) -> bool:
    if not isinstance(valor, str) or not valor.strip():
        erros.append(f"{contexto}: timestamp ausente ou inválido")
        return False
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        erros.append(f"{contexto}: timestamp ausente ou inválido")
        return False
    if instante.tzinfo is None or instante.utcoffset() is None:
        erros.append(f"{contexto}: timestamp precisa incluir fuso horário")
        return False
    return True


def _texto_configuravel(
    valor: Any,
    contexto: str,
    *,
    operacional: bool,
    erros: list[str],
) -> str | None:
    if valor == PLACEHOLDER:
        if operacional:
            erros.append(
                f"{contexto}: placeholder {PLACEHOLDER!r} proibido em fila operacional"
            )
        return None
    if not isinstance(valor, str) or not valor or valor != valor.strip():
        erros.append(
            f"{contexto}: valor não vazio obrigatório; use {PLACEHOLDER!r} somente na fila-base vazia"
        )
        return None
    return valor


def _validar_politica(
    fila: Mapping[str, Any],
    canal: str,
    *,
    operacional: bool,
    erros: list[str],
) -> PoliticaValidada:
    contexto = canal
    erro(
        type(fila.get("schema_version")) is int
        and fila.get("schema_version") == SCHEMA_VERSION,
        f"{contexto}: schema_version deve ser exatamente {SCHEMA_VERSION}",
        erros,
    )
    erro(fila.get("projeto") == PROJETO, f"{contexto}: projeto deve ser {PROJETO!r}", erros)
    erro(fila.get("canal") == canal, f"{contexto}: canal deve ser {canal!r}", erros)
    erro(fila.get("timezone") == TIMEZONE, f"{contexto}: timezone deve ser {TIMEZONE!r}", erros)

    github = _mapa(fila.get("github"), f"{contexto}.github", erros)
    repositorio = _texto_configuravel(
        github.get("repositorio"),
        f"{contexto}.github.repositorio",
        operacional=operacional,
        erros=erros,
    )
    if repositorio is not None:
        erro(
            bool(REPOSITORIO_RE.fullmatch(repositorio)),
            f"{contexto}.github.repositorio: use dono/repositorio",
            erros,
        )
        if not REPOSITORIO_RE.fullmatch(repositorio):
            repositorio = None
    release_tag = _texto_configuravel(
        github.get("release_tag"),
        f"{contexto}.github.release_tag",
        operacional=operacional,
        erros=erros,
    )

    politica = _mapa(fila.get("politica"), f"{contexto}.politica", erros)
    erro(
        politica.get("exigir_aprovacao_por_item") is True,
        f"{contexto}.politica.exigir_aprovacao_por_item deve ser true",
        erros,
    )
    d0_bruto = politica.get("data_inicio_aquecimento")
    if d0_bruto == PLACEHOLDER:
        if operacional:
            erros.append(
                f"{contexto}.politica.data_inicio_aquecimento: placeholder proibido em fila operacional"
            )
        d0 = None
    else:
        d0 = _data_iso(
            d0_bruto,
            f"{contexto}.politica.data_inicio_aquecimento",
            erros,
        )

    reels = _mapa(politica.get("reels"), f"{contexto}.politica.reels", erros)
    horarios = reels.get("horarios")
    erro(
        isinstance(horarios, list) and tuple(horarios) == HORARIOS_REELS,
        f"{contexto}.politica.reels.horarios deve ser o prefixo-base {list(HORARIOS_REELS)!r}",
        erros,
    )
    rampa = reels.get("quantidade_por_semana")
    erro(
        isinstance(rampa, list)
        and all(type(valor) is int for valor in rampa)
        and tuple(rampa) == RAMPA_REELS,
        f"{contexto}.politica.reels.quantidade_por_semana deve ser {list(RAMPA_REELS)!r}",
        erros,
    )
    erro(
        type(reels.get("quantidade_apos_semana_5")) is int
        and reels.get("quantidade_apos_semana_5") == len(HORARIOS_REELS),
        f"{contexto}.politica.reels.quantidade_apos_semana_5 deve ser {len(HORARIOS_REELS)}",
        erros,
    )
    minimo = _decimal(
        reels.get("duracao_minima_segundos"),
        f"{contexto}.politica.reels.duracao_minima_segundos",
        erros,
    )
    maximo = _decimal(
        reels.get("duracao_maxima_segundos"),
        f"{contexto}.politica.reels.duracao_maxima_segundos",
        erros,
    )
    erro(minimo == MINIMO_REEL, f"{contexto}: duração mínima de Reel deve ser 4s", erros)
    erro(maximo == MAXIMO_REEL, f"{contexto}: duração máxima de Reel deve ser 180s", erros)
    erro(
        reels.get("compartilhar_no_feed_instagram") is True,
        f"{contexto}.politica.reels.compartilhar_no_feed_instagram deve ser true",
        erros,
    )
    prefixos = reels.get("prefixos_de_arquivo_bloqueados")
    erro(
        isinstance(prefixos, list)
        and all(isinstance(valor, str) and valor for valor in prefixos)
        and PREFIXO_BLOQUEADO in prefixos,
        f"{contexto}: o prefixo {PREFIXO_BLOQUEADO!r} deve permanecer bloqueado",
        erros,
    )
    legenda = _texto_configuravel(
        reels.get("legenda"),
        f"{contexto}.politica.reels.legenda",
        operacional=operacional,
        erros=erros,
    )
    if legenda is not None:
        erro(
            bool(LEGENDA_RE.fullmatch(legenda)),
            f"{contexto}.politica.reels.legenda deve ser exatamente 'Siga @handle'",
            erros,
        )
        if not LEGENDA_RE.fullmatch(legenda):
            legenda = None

    stories = _mapa(politica.get("stories"), f"{contexto}.politica.stories", erros)
    erro(
        type(stories.get("videos_fonte_por_dia")) is int
        and stories.get("videos_fonte_por_dia") == 1,
        f"{contexto}.politica.stories.videos_fonte_por_dia deve ser 1",
        erros,
    )
    erro(
        stories.get("horario") == HORARIO_STORY,
        f"{contexto}.politica.stories.horario deve ser {HORARIO_STORY}",
        erros,
    )
    limite = _decimal(
        stories.get("limite_parte_segundos"),
        f"{contexto}.politica.stories.limite_parte_segundos",
        erros,
    )
    erro(
        limite is not None and Decimal("0") < limite <= LIMITE_STORY,
        f"{contexto}: limite de Story deve ser positivo e nunca ultrapassar 59s",
        erros,
    )
    erro(
        stories.get("dividir_em_partes_iguais") is True,
        f"{contexto}.politica.stories.dividir_em_partes_iguais deve ser true",
        erros,
    )

    if canal == CANAL_STORIES:
        erro(
            fila.get("horario") == HORARIO_STORY,
            f"{contexto}.horario deve ser {HORARIO_STORY}",
            erros,
        )
        limite_raiz = _decimal(
            fila.get("limite_por_parte_segundos"),
            f"{contexto}.limite_por_parte_segundos",
            erros,
        )
        erro(
            limite_raiz is not None
            and Decimal("0") < limite_raiz <= LIMITE_STORY,
            f"{contexto}: limite declarado na fila nunca pode ultrapassar 59s",
            erros,
        )
        if limite is not None and limite_raiz is not None:
            erro(
                limite_raiz == limite,
                f"{contexto}: limites de Story da raiz e da política divergem",
                erros,
            )

    limite_efetivo = min(
        (valor for valor in (limite, LIMITE_STORY) if valor is not None),
        default=LIMITE_STORY,
    )
    return PoliticaValidada(repositorio, release_tag, d0, legenda, limite_efetivo)


def _registrar_unico(
    valor: str,
    vistos: set[str],
    contexto: str,
    tipo: str,
    erros: list[str],
) -> None:
    if not valor:
        erros.append(f"{contexto}: {tipo} ausente")
        return
    if valor in vistos:
        erros.append(f"{contexto}: {tipo} duplicado: {valor}")
    vistos.add(valor)


def _validar_estado_plataforma(
    nodo: Mapping[str, Any],
    plataforma: str,
    contexto: str,
    erros: list[str],
    *,
    story: bool = False,
) -> str:
    """Confere o estado de UMA plataforma deste item.

    Nada aqui fala da fila inteira, só deste item. Por isso quem chama guarda o
    que sai daqui na lista de defeitos do item, e não na lista que derruba a
    fila toda (regra nova depois do incidente de 12/09/2026).
    """

    estado = _mapa(nodo.get(plataforma), f"{contexto}.{plataforma}", erros)
    status = estado.get("status")
    permitidos = ESTADOS_INSTAGRAM if plataforma == "instagram" else ESTADOS_FACEBOOK
    if status not in permitidos:
        erros.append(
            f"{contexto}.{plataforma}: status inválido; use {sorted(permitidos)!r}"
        )
        return ""
    fase = estado.get("fase")
    fases_permitidas = FASES_INSTAGRAM if plataforma == "instagram" else FASES_FACEBOOK
    if fase is not None:
        erro(
            isinstance(fase, str) and fase in fases_permitidas,
            f"{contexto}.{plataforma}: fase desconhecida ou incompatível",
            erros,
        )
    if status == "pendente":
        erro(
            fase is None,
            f"{contexto}.{plataforma}: item pendente não pode declarar fase",
            erros,
        )
        for identificador in ("container_id", "video_id", "upload_url", "id"):
            erro(
                identificador not in estado,
                f"{contexto}.{plataforma}: item pendente contém {identificador} residual",
                erros,
            )
    if status == "enviando":
        erro(
            fase in {"sessao_criada", "upload_em_andamento"},
            f"{contexto}.facebook: fase incompatível com envio",
            erros,
        )
    if status == "processando":
        fases_processando = (
            {"container_criado", "publicacao_solicitada", "publicado_sem_media_id"}
            if plataforma == "instagram"
            else {"upload_concluido", "publicacao_solicitada", "publicacao_aceita"}
        )
        erro(
            fase in fases_processando,
            f"{contexto}.{plataforma}: fase incompatível com processamento",
            erros,
        )
    alerta_manual = estado.get("reconciliacao_manual")
    if alerta_manual is not None:
        alerta = _mapa(
            alerta_manual, f"{contexto}.{plataforma}.reconciliacao_manual", erros
        )
        erro(
            plataforma == "instagram" and fase == "publicado_sem_media_id",
            f"{contexto}.{plataforma}: alerta manual incompatível com a fase",
            erros,
        )
        erro(
            alerta.get("necessaria") is True,
            f"{contexto}.{plataforma}: reconciliação manual precisa estar ativa",
            erros,
        )
        erro(
            isinstance(alerta.get("motivo"), str) and bool(alerta["motivo"].strip()),
            f"{contexto}.{plataforma}: motivo da reconciliação manual ausente",
            erros,
        )
        _timestamp(
            alerta.get("detectada_em"),
            f"{contexto}.{plataforma}.reconciliacao_manual.detectada_em",
            erros,
        )
    if status == "processando":
        identificador = "container_id" if plataforma == "instagram" else "video_id"
        erro(
            isinstance(estado.get(identificador), str) and bool(estado[identificador]),
            f"{contexto}.{plataforma}: {identificador} obrigatório ao processar",
            erros,
        )
    elif status == "enviando":
        erro(
            isinstance(estado.get("video_id"), str) and bool(estado["video_id"]),
            f"{contexto}.facebook: video_id obrigatório durante envio",
            erros,
        )
    elif status == "erro":
        erro(
            isinstance(estado.get("erro"), str) and bool(estado["erro"].strip()),
            f"{contexto}.{plataforma}: detalhe do erro obrigatório",
            erros,
        )
        _timestamp(
            estado.get("ultima_tentativa_em"),
            f"{contexto}.{plataforma}.ultima_tentativa_em",
            erros,
        )
    elif status == "publicado":
        erro(
            isinstance(estado.get("id"), str) and bool(estado["id"]),
            f"{contexto}.{plataforma}: id obrigatório quando publicado",
            erros,
        )
        _timestamp(
            estado.get("publicado_em"),
            f"{contexto}.{plataforma}.publicado_em",
            erros,
        )
        erro("erro" not in estado, f"{contexto}.{plataforma}: erro residual após publicação", erros)
        erro(
            "reconciliacao_manual" not in estado,
            f"{contexto}.{plataforma}: alerta de reconciliação residual após publicação",
            erros,
        )
        erro(
            fase == "publicacao_confirmada",
            f"{contexto}.{plataforma}: publicação confirmada exige fase canônica",
            erros,
        )
        if plataforma == "instagram" and estado.get("container_id"):
            erro(
                estado.get("id") != estado.get("container_id"),
                f"{contexto}.instagram: id publicado não pode ser o container_id",
                erros,
            )
        if plataforma == "facebook" and story and estado.get("video_id"):
            erro(
                estado.get("id") != estado.get("video_id"),
                f"{contexto}.facebook: Story publicado exige post_id distinto do video_id",
                erros,
            )

    if plataforma == "facebook" and status in {"enviando", "processando", "erro"}:
        video_id = estado.get("video_id")
        upload_url = estado.get("upload_url")
        if status in {"enviando", "processando"} or video_id or upload_url:
            erro(
                isinstance(video_id, str) and bool(video_id),
                f"{contexto}.facebook: video_id obrigatório para retomar a sessão",
                erros,
            )
            erro(
                isinstance(upload_url, str) and bool(upload_url),
                f"{contexto}.facebook: upload_url obrigatória para retomar a sessão",
                erros,
            )
            if isinstance(video_id, str) and isinstance(upload_url, str):
                erro(
                    _upload_url_facebook_valida(upload_url, video_id),
                    f"{contexto}.facebook: upload_url não é a URL canônica da Meta",
                    erros,
                )
    return str(status)


def _validar_midia(
    midia_bruta: Any,
    contexto: str,
    politica: PoliticaValidada,
    *,
    minimo: Decimal,
    maximo: Decimal,
    confirmado: bool,
    erros: list[str],
) -> tuple[Decimal | None, str, str]:
    """Confere a mídia de UM item e devolve (duração, asset, SHA-256).

    O asset e o SHA voltam para quem chamou porque a duplicidade deles vale para
    a fila inteira e continua sendo motivo de parada. Todo o resto que sai daqui
    é defeito de um item só, então vira aviso (12/09/2026).
    """

    midia = _mapa(midia_bruta, f"{contexto}.midia", erros)
    asset_bruto = midia.get("asset")
    asset = asset_bruto.strip() if isinstance(asset_bruto, str) else ""
    erro(
        bool(asset) and asset == asset_bruto,
        f"{contexto}: asset ausente ou com espaços externos",
        erros,
    )

    sha_bruto = midia.get("sha256")
    sha = sha_bruto if isinstance(sha_bruto, str) else ""
    sha_valido = bool(HASH_RE.fullmatch(sha))
    erro(sha_valido, f"{contexto}: SHA-256 deve ter 64 caracteres hexadecimais minúsculos", erros)
    if sha_valido and asset:
        erro(
            asset == f"sha256-{sha}.mp4",
            f"{contexto}: nome do asset deve ser derivado do SHA-256",
            erros,
        )

    tamanho = midia.get("tamanho_bytes")
    erro(
        type(tamanho) is int and tamanho > 0,
        f"{contexto}: tamanho_bytes deve ser inteiro positivo",
        erros,
    )
    duracao = _decimal(midia.get("duracao_segundos"), f"{contexto}.duracao_segundos", erros)
    erro(
        duracao is not None and minimo <= duracao <= maximo,
        f"{contexto}: duração deve ficar entre {minimo}s e {maximo}s",
        erros,
    )

    url = midia.get("url_publica")
    erro(
        isinstance(url, str) and url.startswith("https://"),
        f"{contexto}: url_publica deve usar HTTPS",
        erros,
    )
    if politica.repositorio and politica.release_tag and asset:
        esperada = (
            f"https://github.com/{politica.repositorio}/releases/download/"
            f"{quote(politica.release_tag, safe='')}/{quote(asset, safe='')}"
        )
        erro(url == esperada, f"{contexto}: url_publica diverge de github/release/asset", erros)

    if "removido_da_release_em" in midia:
        _timestamp(
            midia.get("removido_da_release_em"),
            f"{contexto}.midia.removido_da_release_em",
            erros,
        )
        erro(
            confirmado,
            f"{contexto}: asset só pode estar removido após ambas as plataformas publicarem",
            erros,
        )
    if "remocao_solicitada_em" in midia:
        _timestamp(
            midia.get("remocao_solicitada_em"),
            f"{contexto}.midia.remocao_solicitada_em",
            erros,
        )
        erro(
            confirmado,
            f"{contexto}: remoção só pode ser solicitada após ambas as plataformas publicarem",
            erros,
        )
        erro(
            midia.get("remocao_repositorio") == politica.repositorio
            and midia.get("remocao_release_tag") == politica.release_tag,
            f"{contexto}: intenção de remoção diverge do repositório/Release da política",
            erros,
        )
    return duracao, asset, sha


def _validar_origem(
    origem_bruta: Any,
    contexto: str,
    erros: list[str],
) -> tuple[str, str]:
    """Confere a origem de UM item e devolve (arquivo, SHA-256).

    O SHA volta para quem chamou registrar a unicidade, que é checagem da fila
    inteira; o formato em si é defeito de um item só.
    """

    origem = _mapa(origem_bruta, f"{contexto}.origem", erros)
    arquivo = origem.get("arquivo")
    arquivo_valido = (
        isinstance(arquivo, str)
        and bool(arquivo.strip())
        and arquivo == arquivo.strip()
        and "/" not in arquivo
        and "\\" not in arquivo
    )
    erro(arquivo_valido, f"{contexto}: origem.arquivo deve conter somente o nome-base", erros)
    sha = origem.get("sha256") if isinstance(origem.get("sha256"), str) else ""
    erro(bool(HASH_RE.fullmatch(sha)), f"{contexto}: origem.sha256 inválido", erros)
    return (str(arquivo) if isinstance(arquivo, str) else "", sha)


def _validar_status_item(
    item: Mapping[str, Any],
    contexto: str,
    *,
    todos_publicados: bool,
    erros: list[str],
    defeitos: list[str],
) -> None:
    # Status fora da lista permitida é doença da fila: ninguém sabe mais em que
    # pé o item está, então derruba tudo. Já "concluído sem as duas
    # confirmações" é problema de um item só e vira aviso.
    status = item.get("status")
    erro(status in STATUS_ITENS, f"{contexto}: status deve ser 'pendente' ou 'concluido'", erros)
    if status == "concluido":
        erro(
            todos_publicados,
            f"{contexto}: concluído exige publicação nas duas plataformas",
            defeitos,
        )
        _timestamp(item.get("concluido_em"), f"{contexto}.concluido_em", defeitos)


def _conferir_reel(
    item: Mapping[str, Any],
    contexto: str,
    politica: PoliticaValidada,
    unicidade: Unicidade,
    *,
    erros: list[str],
    defeitos: list[str],
) -> None:
    """Confere tudo o que diz respeito a UM Reel.

    Em 12/09/2026 cinco vídeos agendados para novembro deixaram um canal sem
    publicar de manhã: o conferidor reprovava a fila inteira por causa deles e o
    publicador nem chegava a rodar. Por isso o que é defeito de um item só cai
    em `defeitos` (vira aviso, e o publicador recusa aquele horário na hora de
    publicar) e o que estraga a confiança na fila toda cai em `erros`.
    """

    erro(item.get("aprovado") is True, f"{contexto}: aprovado deve ser true", defeitos)

    arquivo_origem, sha_origem = _validar_origem(item.get("origem"), contexto, defeitos)
    if arquivo_origem:
        erro(
            not arquivo_origem.casefold().startswith(PREFIXO_BLOQUEADO.casefold()),
            f"{contexto}: origem com prefixo bloqueado {PREFIXO_BLOQUEADO!r}",
            defeitos,
        )
    if sha_origem:
        _registrar_unico(sha_origem, unicidade.shas_origens, contexto, "SHA-256 de origem", erros)

    status_instagram = _validar_estado_plataforma(item, "instagram", contexto, defeitos)
    status_facebook = _validar_estado_plataforma(item, "facebook", contexto, defeitos)
    ambos_publicados = status_instagram == status_facebook == "publicado"
    _validar_status_item(
        item,
        contexto,
        todos_publicados=ambos_publicados,
        erros=erros,
        defeitos=defeitos,
    )

    instagram = item.get("instagram") if isinstance(item.get("instagram"), Mapping) else {}
    facebook = item.get("facebook") if isinstance(item.get("facebook"), Mapping) else {}
    legenda_ig = instagram.get("legenda")
    legenda_fb = facebook.get("legenda")
    erro(
        isinstance(legenda_ig, str)
        and legenda_ig == legenda_fb
        and bool(LEGENDA_RE.fullmatch(legenda_ig)),
        f"{contexto}: legendas devem ser idênticas e usar exatamente 'Siga @handle'",
        defeitos,
    )
    if politica.legenda_reels is not None:
        erro(
            legenda_ig == politica.legenda_reels,
            f"{contexto}: legenda diverge da política versionada",
            defeitos,
        )
    erro(
        instagram.get("share_to_feed") is True,
        f"{contexto}: instagram.share_to_feed deve ser true",
        defeitos,
    )
    _, asset, sha_midia = _validar_midia(
        item.get("midia"),
        contexto,
        politica,
        minimo=MINIMO_REEL,
        maximo=MAXIMO_REEL,
        confirmado=ambos_publicados,
        erros=defeitos,
    )
    # Asset ou SHA repetido é da mesma família do "ID repetido": duas linhas da
    # fila apontando para o mesmo vídeo. Isso continua derrubando tudo.
    if asset:
        _registrar_unico(asset, unicidade.assets, contexto, "asset", erros)
    if sha_midia:
        _registrar_unico(sha_midia, unicidade.shas_midias, contexto, "SHA-256 de mídia", erros)


def _validar_itens_reels(
    fila: Mapping[str, Any],
    politica: PoliticaValidada,
    unicidade: Unicidade,
    erros: list[str],
) -> dict[str, str]:
    """Confere a fila de Reels e devolve os defeitos, por item, que viram aviso."""

    conteudos = _lista(fila.get("conteudos"), "fila-reels.conteudos", erros)
    slots: set[tuple[str, str]] = set()
    horarios_por_data: dict[date, set[str]] = {}
    defeitos: dict[str, str] = {}
    for indice, bruto in enumerate(conteudos, 1):
        contexto = f"Reel #{indice}"
        item = _mapa(bruto, contexto, erros)

        identificador = item.get("id") if isinstance(item.get("id"), str) else ""
        _registrar_unico(identificador, unicidade.ids, contexto, "ID", erros)
        data_texto = item.get("data")
        data_item = _data_iso(data_texto, f"{contexto}.data", erros)
        horario = item.get("horario") if isinstance(item.get("horario"), str) else ""
        erro(horario in HORARIOS_REELS, f"{contexto}: horário fora dos slots oficiais", erros)
        slot = (str(data_texto), horario)
        erro(slot not in slots, f"{contexto}: slot duplicado {slot}", erros)
        slots.add(slot)
        if data_item is not None and horario in HORARIOS_REELS:
            horarios_por_data.setdefault(data_item, set()).add(horario)

        motivos: list[str] = []
        _conferir_reel(item, contexto, politica, unicidade, erros=erros, defeitos=motivos)
        if motivos:
            defeitos[identificador or contexto] = "; ".join(motivos)

    if politica.d0 is None:
        return defeitos
    for dia, presentes_set in sorted(horarios_por_data.items()):
        if dia < politica.d0:
            erros.append(f"Reels de {dia}: data anterior ao D0 {politica.d0}")
            continue
        semana = ((dia - politica.d0).days // 7) + 1
        quantidade = min(semana, len(HORARIOS_REELS))
        ativos = HORARIOS_REELS[:quantidade]
        presentes = tuple(horario for horario in HORARIOS_REELS if horario in presentes_set)
        if presentes != ativos[: len(presentes)]:
            erros.append(
                f"Reels de {dia}: slots {list(presentes)!r} quebram a rampa/prefixo {list(ativos)!r}"
            )
    return defeitos


def _conferir_story(
    pacote: Mapping[str, Any],
    contexto: str,
    politica: PoliticaValidada,
    unicidade: Unicidade,
    *,
    erros: list[str],
    defeitos: list[str],
) -> None:
    """Confere tudo o que diz respeito a UM pacote de Stories.

    Mesma separação dos Reels: defeito do pacote vira aviso e é o publicador que
    recusa aquele horário; só a duplicidade de asset/SHA, que é doença da fila
    inteira, continua derrubando tudo (12/09/2026).
    """

    erro(pacote.get("aprovado") is True, f"{contexto}: aprovado deve ser true", defeitos)

    _, sha_origem = _validar_origem(pacote.get("origem"), contexto, defeitos)
    if sha_origem:
        _registrar_unico(sha_origem, unicidade.shas_origens, contexto, "SHA-256 de origem", erros)

    partes = _lista(pacote.get("partes"), f"{contexto}.partes", defeitos)
    erro(bool(partes), f"{contexto}: pacote não pode ficar vazio", defeitos)
    ordens = [parte.get("ordem") if isinstance(parte, Mapping) else None for parte in partes]
    erro(
        ordens == list(range(1, len(partes) + 1)),
        f"{contexto}: partes devem estar em ordem contínua a partir de 1",
        defeitos,
    )

    todas_publicadas = True
    duracoes: list[Decimal] = []
    for numero, parte_bruta in enumerate(partes, 1):
        parte = _mapa(parte_bruta, f"{contexto}, parte {numero}", defeitos)
        ordem = parte.get("ordem", numero)
        pctx = f"{contexto}, parte {ordem}"
        status_instagram = _validar_estado_plataforma(
            parte, "instagram", pctx, defeitos, story=True
        )
        status_facebook = _validar_estado_plataforma(
            parte, "facebook", pctx, defeitos, story=True
        )
        confirmada = status_instagram == status_facebook == "publicado"
        todas_publicadas = todas_publicadas and confirmada
        duracao, asset, sha_midia = _validar_midia(
            parte.get("midia"),
            pctx,
            politica,
            minimo=Decimal("0.000001"),
            maximo=min(politica.limite_story, LIMITE_STORY),
            confirmado=confirmada,
            erros=defeitos,
        )
        if asset:
            _registrar_unico(asset, unicidade.assets, pctx, "asset", erros)
        if sha_midia:
            _registrar_unico(sha_midia, unicidade.shas_midias, pctx, "SHA-256 de mídia", erros)
        if duracao is not None and duracao > 0:
            duracoes.append(duracao)

    if len(duracoes) > 1:
        erro(
            max(duracoes) - min(duracoes) <= Decimal("0.25"),
            f"{contexto}: partes não estão iguais dentro da tolerância de 0,25s",
            defeitos,
        )
    _validar_status_item(
        pacote,
        contexto,
        todos_publicados=todas_publicadas and bool(partes),
        erros=erros,
        defeitos=defeitos,
    )


def _validar_itens_stories(
    fila: Mapping[str, Any],
    politica: PoliticaValidada,
    unicidade: Unicidade,
    erros: list[str],
) -> dict[str, str]:
    """Confere a fila de Stories e devolve os defeitos, por pacote, que viram aviso."""

    pacotes = _lista(fila.get("pacotes"), "fila-stories.pacotes", erros)
    datas: set[str] = set()
    slots: set[tuple[str, str]] = set()
    defeitos: dict[str, str] = {}
    for indice, bruto in enumerate(pacotes, 1):
        contexto = f"Story #{indice}"
        pacote = _mapa(bruto, contexto, erros)

        identificador = pacote.get("id") if isinstance(pacote.get("id"), str) else ""
        _registrar_unico(identificador, unicidade.ids, contexto, "ID", erros)
        data_texto = pacote.get("data")
        data_item = _data_iso(data_texto, f"{contexto}.data", erros)
        horario = pacote.get("horario")
        erro(horario == HORARIO_STORY, f"{contexto}: horário deve ser 09:00", erros)
        slot = (str(data_texto), str(horario))
        erro(slot not in slots, f"{contexto}: slot duplicado {slot}", erros)
        slots.add(slot)
        data_chave = str(data_texto)
        erro(data_chave not in datas, f"{contexto}: somente um vídeo-fonte por dia", erros)
        datas.add(data_chave)
        if data_item is not None and politica.d0 is not None:
            erro(
                data_item >= politica.d0,
                f"{contexto}: data anterior ao D0 {politica.d0}",
                erros,
            )

        motivos: list[str] = []
        _conferir_story(pacote, contexto, politica, unicidade, erros=erros, defeitos=motivos)
        if motivos:
            defeitos[identificador or contexto] = "; ".join(motivos)
    return defeitos


def conferir_filas(
    fila_reels: Mapping[str, Any], fila_stories: Mapping[str, Any]
) -> tuple[list[str], dict[str, str]]:
    """Confere as duas filas juntas e separa os dois tipos de problema.

    Devolve (erros que derrubam a fila, defeitos por item). Erro é coisa que tira
    a confiança da fila inteira: cabeçalho, política, ID/asset/SHA repetido,
    data, janela e status fora da lista. Defeito é problema de um item só, que
    apenas tira aquele item da publicação.
    """

    erros: list[str] = []
    operacional = bool(fila_reels.get("conteudos")) or bool(fila_stories.get("pacotes"))
    politica_reels = _validar_politica(
        fila_reels,
        CANAL_REELS,
        operacional=operacional,
        erros=erros,
    )
    politica_stories = _validar_politica(
        fila_stories,
        CANAL_STORIES,
        operacional=operacional,
        erros=erros,
    )
    erro(
        fila_reels.get("github") == fila_stories.get("github"),
        "Filas: configurações github divergentes",
        erros,
    )
    erro(
        fila_reels.get("politica") == fila_stories.get("politica"),
        "Filas: políticas versionadas divergentes",
        erros,
    )
    unicidade = Unicidade()
    defeitos = _validar_itens_reels(fila_reels, politica_reels, unicidade, erros)
    defeitos.update(_validar_itens_stories(fila_stories, politica_stories, unicidade, erros))
    return erros, defeitos


def validar_filas(
    fila_reels: Mapping[str, Any], fila_stories: Mapping[str, Any]
) -> list[str]:
    """Devolve só o que derruba a fila inteira. Defeito de item sai em conferir_filas()."""

    return conferir_filas(fila_reels, fila_stories)[0]


def _contexto_do_slot(item: Mapping[str, Any], tipo: str) -> str:
    identificador = item.get("id")
    return f"{tipo} {identificador}" if isinstance(identificador, str) and identificador else tipo


def defeito_do_reel(fila: Mapping[str, Any], item: Mapping[str, Any]) -> str | None:
    """Devolve o motivo que impede publicar SÓ este Reel, ou None se ele está bom.

    É isto que o publicar_reels.py chama na hora de publicar. O conferidor da
    fila só avisa sobre item ruim para não calar o canal inteiro, então a recusa
    de verdade acontece aqui, e só o horário deste item fica sem publicar.
    """

    descartados: list[str] = []
    politica = _validar_politica(fila, CANAL_REELS, operacional=True, erros=descartados)
    motivos: list[str] = []
    # A mesma lista nos dois lados: conferindo um item sozinho, qualquer coisa
    # que apareça é motivo para não publicar justamente ele.
    _conferir_reel(
        item,
        _contexto_do_slot(item, "Reel"),
        politica,
        Unicidade(),
        erros=motivos,
        defeitos=motivos,
    )
    return "; ".join(motivos) if motivos else None


def defeito_do_story(fila: Mapping[str, Any], pacote: Mapping[str, Any]) -> str | None:
    """Devolve o motivo que impede publicar SÓ este pacote de Stories, ou None."""

    descartados: list[str] = []
    politica = _validar_politica(fila, CANAL_STORIES, operacional=True, erros=descartados)
    motivos: list[str] = []
    _conferir_story(
        pacote,
        _contexto_do_slot(pacote, "Story"),
        politica,
        Unicidade(),
        erros=motivos,
        defeitos=motivos,
    )
    return "; ".join(motivos) if motivos else None


def validar_reels(fila: Mapping[str, Any], erros: list[str]) -> dict[str, str]:
    """Compatibilidade para validações unitárias de uma fila de Reels.

    Enche `erros` com o que derruba a fila e devolve os defeitos por item, que
    são só aviso.
    """

    politica = _validar_politica(
        fila,
        CANAL_REELS,
        operacional=bool(fila.get("conteudos")),
        erros=erros,
    )
    return _validar_itens_reels(fila, politica, Unicidade(), erros)


def validar_stories(fila: Mapping[str, Any], erros: list[str]) -> dict[str, str]:
    """Compatibilidade para validações unitárias de uma fila de Stories.

    Enche `erros` com o que derruba a fila e devolve os defeitos por pacote, que
    são só aviso.
    """

    politica = _validar_politica(
        fila,
        CANAL_STORIES,
        operacional=bool(fila.get("pacotes")),
        erros=erros,
    )
    return _validar_itens_stories(fila, politica, Unicidade(), erros)


def _carregar(caminho: Path) -> Mapping[str, Any]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"JSON inválido ou indisponível: {caminho}") from exc
    if not isinstance(dados, Mapping):
        raise RuntimeError(f"A raiz de {caminho} deve ser um objeto JSON.")
    return dados


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Valida offline as filas IG/FB.")
    parser.add_argument("--raiz", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    reels = _carregar(args.raiz / "fila" / "fila-reels.json")
    stories = _carregar(args.raiz / "fila" / "fila-stories.json")
    erros, defeitos = conferir_filas(reels, stories)
    if erros:
        print("\n".join(erros), file=sys.stderr)
        return 1
    print(
        f"OK: {len(reels.get('conteudos', []))} Reels e "
        f"{len(stories.get('pacotes', []))} pacotes de Stories válidos."
    )
    # Defeito de item é aviso, não parada: um vídeo errado agendado para daqui a
    # dois meses não pode impedir a publicação de hoje (12/09/2026). Quem recusa
    # o item ruim é o publicador, no horário dele.
    if defeitos:
        print(f"AVISO: {len(defeitos)} item(ns) com defeito; serão pulados na publicação:")
        for identificador, motivo in sorted(defeitos.items()):
            print(f"  - {identificador}: {motivo}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


__all__ = [
    "CANAL_REELS",
    "CANAL_STORIES",
    "HORARIOS_REELS",
    "PLACEHOLDER",
    "PROJETO",
    "RAMPA_REELS",
    "SCHEMA_VERSION",
    "TIMEZONE",
    "conferir_filas",
    "defeito_do_reel",
    "defeito_do_story",
    "erro",
    "main",
    "validar_filas",
    "validar_reels",
    "validar_stories",
]
