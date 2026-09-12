"""Auditoria somente-leitura dos assets transitórios da Release.

O script compara os metadados dos vídeos ainda necessários pelas filas com os
metadados devolvidos pela API do GitHub. Assets que já não atendem a nenhuma
publicação pendente são apenas informados como órfãos; nenhuma operação de
remoção ou alteração é executada aqui.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import requests


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PLATAFORMAS = ("instagram", "facebook")
TAG_PADRAO = "fila-instagram-facebook"


@dataclass(frozen=True, slots=True)
class ReferenciaAsset:
    """Uma mídia citada por uma unidade de publicação da fila."""

    nome: str
    sha256: str
    tamanho_bytes: int | None
    contexto: str
    pendente: bool


@dataclass(frozen=True, slots=True)
class AssetRelease:
    """Metadados verificáveis de um asset devolvido pela API do GitHub."""

    nome: str
    sha256: str
    tamanho_bytes: int | None


@dataclass(frozen=True, slots=True)
class ProblemaAsset:
    nome: str
    contexto: str
    motivo: str


@dataclass(frozen=True, slots=True)
class ResultadoAuditoria:
    referencias: tuple[ReferenciaAsset, ...]
    assets_release: tuple[AssetRelease, ...]
    ausentes: tuple[ProblemaAsset, ...]
    divergentes: tuple[ProblemaAsset, ...]
    orfaos: tuple[AssetRelease, ...]
    bytes_release: int
    bytes_pendentes: int
    bytes_orfaos: int

    @property
    def falhou(self) -> bool:
        """Órfãos não tornam a auditoria inválida."""

        return bool(self.ausentes or self.divergentes)


HttpGet = Callable[..., Any]


def _objeto_json(caminho: Path) -> dict[str, Any]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as erro:
        raise RuntimeError(f"Fila JSON inválida ou indisponível: {caminho}") from erro
    if not isinstance(dados, dict):
        raise RuntimeError(f"A fila precisa ter um objeto JSON na raiz: {caminho}")
    return dados


def carregar_filas(raiz: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Carrega as duas filas oficiais sem criar ou modificar arquivos."""

    pasta = raiz / "fila"
    return (
        _objeto_json(pasta / "fila-reels.json"),
        _objeto_json(pasta / "fila-stories.json"),
    )


def _confirmado_em_ambas(nodo: Mapping[str, Any]) -> bool:
    return all(
        isinstance(nodo.get(plataforma), Mapping)
        and nodo[plataforma].get("status") == "publicado"
        for plataforma in PLATAFORMAS
    )


def _numero_bytes(valor: Any) -> int | None:
    if isinstance(valor, bool):
        return None
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    return numero if numero >= 0 else None


def _digest(valor: Any) -> str:
    texto = str(valor or "").strip().lower()
    return texto.removeprefix("sha256:")


def _referencia(
    midia: Any,
    *,
    contexto: str,
    pendente: bool,
) -> ReferenciaAsset:
    dados = midia if isinstance(midia, Mapping) else {}
    return ReferenciaAsset(
        nome=str(dados.get("asset", "")).strip(),
        sha256=_digest(dados.get("sha256")),
        tamanho_bytes=_numero_bytes(dados.get("tamanho_bytes")),
        contexto=contexto,
        pendente=pendente,
    )


def coletar_assets_referenciados(
    fila_reels: Mapping[str, Any],
    fila_stories: Mapping[str, Any],
) -> tuple[ReferenciaAsset, ...]:
    """Coleta mídias de Reels e partes de Stories, inclusive as concluídas.

    ``pendente`` é calculado na unidade que realmente consome o vídeo. Assim,
    uma parte de Story já confirmada nas duas redes pode ter sido removida pela
    limpeza normal mesmo se outra parte do mesmo pacote ainda estiver pendente.
    """

    referencias: list[ReferenciaAsset] = []
    conteudos = fila_reels.get("conteudos", [])
    if not isinstance(conteudos, list):
        raise RuntimeError("fila-reels.json: conteudos precisa ser uma lista.")
    for indice, item in enumerate(conteudos, 1):
        if not isinstance(item, Mapping):
            raise RuntimeError(f"fila-reels.json: Reel #{indice} inválido.")
        identidade = str(item.get("id") or f"#{indice}")
        pendente = (
            item.get("status") != "concluido" and not _confirmado_em_ambas(item)
        )
        referencias.append(
            _referencia(
                item.get("midia"),
                contexto=f"Reel {identidade}",
                pendente=pendente,
            )
        )

    pacotes = fila_stories.get("pacotes", [])
    if not isinstance(pacotes, list):
        raise RuntimeError("fila-stories.json: pacotes precisa ser uma lista.")
    for indice, pacote in enumerate(pacotes, 1):
        if not isinstance(pacote, Mapping):
            raise RuntimeError(f"fila-stories.json: pacote #{indice} inválido.")
        identidade = str(pacote.get("id") or f"#{indice}")
        partes = pacote.get("partes", [])
        if not isinstance(partes, list):
            raise RuntimeError(
                f"fila-stories.json: partes do pacote {identidade} precisa ser uma lista."
            )
        pacote_concluido = pacote.get("status") == "concluido"
        for numero, parte in enumerate(partes, 1):
            if not isinstance(parte, Mapping):
                raise RuntimeError(
                    f"fila-stories.json: parte #{numero} do pacote {identidade} inválida."
                )
            ordem = parte.get("ordem", numero)
            pendente = not pacote_concluido and not _confirmado_em_ambas(parte)
            referencias.append(
                _referencia(
                    parte.get("midia"),
                    contexto=f"Story {identidade}, parte {ordem}",
                    pendente=pendente,
                )
            )
    return tuple(referencias)


def consultar_assets_release(
    repositorio: str,
    tag: str,
    token: str,
    *,
    http_get: HttpGet | None = None,
) -> tuple[AssetRelease, ...]:
    """Consulta todos os assets por paginação autenticada e sem efeitos colaterais."""

    partes_repo = repositorio.strip().split("/")
    if len(partes_repo) != 2 or not all(partes_repo) or any(
        re.search(r"\s", parte) for parte in partes_repo
    ):
        raise RuntimeError("GITHUB_REPOSITORY deve usar o formato proprietario/repositorio.")
    if not tag.strip():
        raise RuntimeError("RELEASE_TAG não pode ficar vazia.")
    if not token.strip():
        raise RuntimeError("GITHUB_TOKEN não foi disponibilizado.")

    obter = http_get or requests.get
    repo_url = "/".join(quote(parte, safe="") for parte in partes_repo)
    url = (
        f"https://api.github.com/repos/{repo_url}/releases/tags/"
        f"{quote(tag.strip(), safe='')}"
    )
    try:
        resposta = obter(
            url,
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            timeout=30,
        )
        resposta.raise_for_status()
        release = resposta.json()
    except Exception as erro:
        raise RuntimeError(
            f"Não foi possível consultar a Release {tag!r} de {repositorio}."
        ) from erro

    if not isinstance(release, Mapping):
        raise RuntimeError("A API do GitHub devolveu metadados de Release inválidos.")
    release_id = release.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int):
        raise RuntimeError("A API do GitHub não identificou a Release solicitada.")

    dados_assets: list[Any] = []
    endpoint_assets = (
        f"https://api.github.com/repos/{repo_url}/releases/{release_id}/assets"
    )
    try:
        for pagina in range(1, 1001):
            resposta_assets = obter(
                endpoint_assets,
                headers={
                    "Authorization": f"Bearer {token.strip()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                params={"per_page": 100, "page": pagina},
                timeout=30,
            )
            resposta_assets.raise_for_status()
            lote = resposta_assets.json()
            if not isinstance(lote, list):
                raise RuntimeError("A API do GitHub não devolveu uma lista de assets.")
            dados_assets.extend(lote)
            if len(lote) < 100:
                break
        else:
            raise RuntimeError("A paginação de assets excedeu o limite defensivo.")
    except Exception as erro:
        raise RuntimeError(
            f"Não foi possível listar todos os assets da Release {tag!r}."
        ) from erro

    assets: list[AssetRelease] = []
    for indice, bruto in enumerate(dados_assets, 1):
        if not isinstance(bruto, Mapping):
            raise RuntimeError(f"A API devolveu o asset #{indice} em formato inválido.")
        assets.append(
            AssetRelease(
                nome=str(bruto.get("name", "")).strip(),
                sha256=_digest(bruto.get("digest")),
                tamanho_bytes=_numero_bytes(bruto.get("size")),
            )
        )
    return tuple(assets)


def _somar_bytes(assets: Sequence[AssetRelease]) -> int:
    return sum(asset.tamanho_bytes or 0 for asset in assets)


def auditar_assets(
    referencias: Sequence[ReferenciaAsset],
    assets_release: Sequence[AssetRelease],
) -> ResultadoAuditoria:
    """Compara referências pendentes com a Release, sem executar I/O."""

    pendentes = tuple(referencia for referencia in referencias if referencia.pendente)
    nomes_pendentes = {referencia.nome for referencia in pendentes if referencia.nome}
    por_nome: dict[str, list[AssetRelease]] = {}
    for asset in assets_release:
        por_nome.setdefault(asset.nome, []).append(asset)

    ausentes: list[ProblemaAsset] = []
    divergentes: list[ProblemaAsset] = []
    for referencia in pendentes:
        if not referencia.nome:
            divergentes.append(
                ProblemaAsset("<sem nome>", referencia.contexto, "nome ausente na fila")
            )
            continue

        encontrados = por_nome.get(referencia.nome, [])
        if not encontrados:
            ausentes.append(
                ProblemaAsset(
                    referencia.nome,
                    referencia.contexto,
                    "não existe na Release",
                )
            )
            continue
        if len(encontrados) != 1:
            divergentes.append(
                ProblemaAsset(
                    referencia.nome,
                    referencia.contexto,
                    f"aparece {len(encontrados)} vezes na resposta da Release",
                )
            )
            continue

        asset = encontrados[0]
        motivos: list[str] = []
        if referencia.tamanho_bytes is None or referencia.tamanho_bytes <= 0:
            motivos.append("tamanho inválido na fila")
        elif asset.tamanho_bytes is None:
            motivos.append("tamanho ausente/inválido na Release")
        elif asset.tamanho_bytes != referencia.tamanho_bytes:
            motivos.append(
                "tamanho divergente "
                f"(fila={referencia.tamanho_bytes}, Release={asset.tamanho_bytes})"
            )

        if not HASH_RE.fullmatch(referencia.sha256):
            motivos.append("SHA-256 inválido na fila")
        elif not HASH_RE.fullmatch(asset.sha256):
            motivos.append("digest SHA-256 ausente/inválido na Release")
        elif asset.sha256 != referencia.sha256:
            motivos.append(
                f"SHA-256 divergente (fila={referencia.sha256}, Release={asset.sha256})"
            )
        if motivos:
            divergentes.append(
                ProblemaAsset(referencia.nome, referencia.contexto, "; ".join(motivos))
            )

    orfaos = tuple(asset for asset in assets_release if asset.nome not in nomes_pendentes)
    vinculados = tuple(asset for asset in assets_release if asset.nome in nomes_pendentes)
    return ResultadoAuditoria(
        referencias=tuple(referencias),
        assets_release=tuple(assets_release),
        ausentes=tuple(ausentes),
        divergentes=tuple(divergentes),
        orfaos=orfaos,
        bytes_release=_somar_bytes(assets_release),
        bytes_pendentes=_somar_bytes(vinculados),
        bytes_orfaos=_somar_bytes(orfaos),
    )


def formatar_bytes(total: int) -> str:
    valor = float(total)
    unidades = ("B", "KiB", "MiB", "GiB", "TiB")
    for unidade in unidades:
        if abs(valor) < 1024 or unidade == unidades[-1]:
            return f"{int(valor)} B" if unidade == "B" else f"{valor:.2f} {unidade}"
        valor /= 1024
    raise AssertionError("unidade de bytes inalcançável")


def linhas_relatorio(resultado: ResultadoAuditoria) -> tuple[str, ...]:
    pendentes = tuple(ref for ref in resultado.referencias if ref.pendente)
    nomes_pendentes = {ref.nome for ref in pendentes if ref.nome}
    linhas = [
        (
            f"Filas: {len(resultado.referencias)} referência(s), "
            f"{len(pendentes)} pendente(s), {len(nomes_pendentes)} asset(s) único(s)."
        ),
        (
            f"Release: {len(resultado.assets_release)} asset(s), "
            f"{formatar_bytes(resultado.bytes_release)} no total; "
            f"{formatar_bytes(resultado.bytes_pendentes)} vinculado(s) a pendências."
        ),
    ]
    for problema in resultado.ausentes:
        linhas.append(
            f"AUSENTE: {problema.nome} — {problema.contexto}: {problema.motivo}."
        )
    for problema in resultado.divergentes:
        linhas.append(
            f"DIVERGENTE: {problema.nome} — {problema.contexto}: {problema.motivo}."
        )
    if resultado.orfaos:
        linhas.append(
            f"AVISO: {len(resultado.orfaos)} asset(s) órfão(s), "
            f"{formatar_bytes(resultado.bytes_orfaos)}; nenhuma remoção foi feita."
        )
        for asset in resultado.orfaos:
            tamanho = formatar_bytes(asset.tamanho_bytes or 0)
            linhas.append(f"ÓRFÃO: {asset.nome or '<sem nome>'} — {tamanho}.")
    else:
        linhas.append("Órfãos: nenhum.")
    if resultado.falhou:
        linhas.append(
            "RESULTADO: FALHA — há asset pendente ausente ou divergente na Release."
        )
    else:
        linhas.append("RESULTADO: OK — todos os assets pendentes estão íntegros.")
    return tuple(linhas)


def main(
    argv: Sequence[str] | None = None,
    *,
    ambiente: Mapping[str, str] | None = None,
    http_get: HttpGet | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Audita, sem alterar, os assets das filas na Release do GitHub."
    )
    parser.add_argument("--raiz", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--repositorio")
    parser.add_argument("--tag")
    args = parser.parse_args(argv)

    env = os.environ if ambiente is None else ambiente
    repositorio = str(args.repositorio or env.get("GITHUB_REPOSITORY", "")).strip()
    tag = str(args.tag or env.get("RELEASE_TAG", TAG_PADRAO)).strip()
    token = str(env.get("GITHUB_TOKEN", "")).strip()
    if not repositorio:
        raise RuntimeError("GITHUB_REPOSITORY não foi disponibilizado.")

    fila_reels, fila_stories = carregar_filas(args.raiz)
    referencias = coletar_assets_referenciados(fila_reels, fila_stories)
    assets = consultar_assets_release(
        repositorio,
        tag,
        token,
        http_get=http_get,
    )
    resultado = auditar_assets(referencias, assets)
    print(f"Auditoria somente-leitura: {repositorio}, Release {tag!r}.")
    for linha in linhas_relatorio(resultado):
        print(linha)
    return 1 if resultado.falhou else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        raise SystemExit(2) from erro


__all__ = [
    "AssetRelease",
    "ProblemaAsset",
    "ReferenciaAsset",
    "ResultadoAuditoria",
    "auditar_assets",
    "carregar_filas",
    "coletar_assets_referenciados",
    "consultar_assets_release",
    "formatar_bytes",
    "linhas_relatorio",
    "main",
]
