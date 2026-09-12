"""Remove somente assets já confirmados no Instagram e no Facebook."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests

from meta_comum import BRT, PLATAFORMAS, persistir_fila


HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def confirmado(nodo: dict) -> bool:
    return all(nodo.get(plataforma, {}).get("status") == "publicado" for plataforma in PLATAFORMAS)


def midias_liberadas(fila: dict) -> list[dict]:
    resultado: list[dict] = []
    for item in fila.get("conteudos", []):
        midia = item.get("midia", {})
        if confirmado(item) and not midia.get("removido_da_release_em"):
            resultado.append(midia)
    for pacote in fila.get("pacotes", []):
        for parte in pacote.get("partes", []):
            midia = parte.get("midia", {})
            if confirmado(parte) and not midia.get("removido_da_release_em"):
                resultado.append(midia)
    return resultado


def _validar_vinculo(fila: dict, repositorio: str, tag: str) -> None:
    github = fila.get("github")
    if not isinstance(github, dict):
        raise RuntimeError("A fila não declara o repositório e a Release operacionais.")
    if github.get("repositorio") != repositorio or github.get("release_tag") != tag:
        raise RuntimeError(
            "A fila não pertence ao GITHUB_REPOSITORY/RELEASE_TAG desta execução."
        )


def _digest_asset(asset: dict) -> str:
    return str(asset.get("digest") or "").strip().lower().removeprefix("sha256:")


def ausencia_autorizada(midia: dict, repositorio: str, tag: str) -> bool:
    return bool(
        midia.get("remocao_solicitada_em")
        and midia.get("remocao_repositorio") == repositorio
        and midia.get("remocao_release_tag") == tag
    )


def _conferir_asset(asset: dict, midia: dict, cabecalhos: dict) -> None:
    nome = str(midia.get("asset") or "")
    digest = str(midia.get("sha256") or "").strip().lower()
    try:
        tamanho = int(midia.get("tamanho_bytes", 0))
    except (TypeError, ValueError):
        tamanho = 0
    if not nome or Path(nome).name != nome or not HASH_RE.fullmatch(digest) or tamanho <= 0:
        raise RuntimeError(f"Metadados inválidos impedem a remoção do asset {nome!r}.")
    if asset.get("name") != nome or int(asset.get("size", -1)) != tamanho:
        raise RuntimeError(f"Tamanho/nome divergente na Release para {nome}.")
    digest_api = _digest_asset(asset)
    if digest_api:
        if digest_api != digest:
            raise RuntimeError(f"SHA-256 divergente na Release para {nome}.")
        return

    # Releases antigas podem não devolver `digest`. Nesse caso a exclusão só
    # prossegue após baixar e calcular o conteúdo atual.
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith("https://"):
        raise RuntimeError(f"A Release não fornece digest nem URL verificável para {nome}.")
    calculado = hashlib.sha256()
    bytes_lidos = 0
    with requests.get(url, headers=cabecalhos, stream=True, timeout=(30, 900)) as resposta:
        resposta.raise_for_status()
        for bloco in resposta.iter_content(chunk_size=1024 * 1024):
            if bloco:
                calculado.update(bloco)
                bytes_lidos += len(bloco)
    if bytes_lidos != tamanho or calculado.hexdigest() != digest:
        raise RuntimeError(f"Conteúdo divergente na Release para {nome}.")


def _listar_todos_assets(
    repositorio: str, release_id: int, cabecalhos: dict
) -> list[dict]:
    """Usa o endpoint paginado; uma Release pode exceder 100 assets."""

    resultado: list[dict] = []
    for pagina in range(1, 1001):
        resposta = requests.get(
            f"https://api.github.com/repos/{repositorio}/releases/{release_id}/assets",
            headers=cabecalhos,
            params={"per_page": 100, "page": pagina},
            timeout=30,
        )
        resposta.raise_for_status()
        lote = resposta.json()
        if not isinstance(lote, list):
            raise RuntimeError("A API do GitHub não devolveu uma lista de assets.")
        if any(not isinstance(asset, dict) for asset in lote):
            raise RuntimeError("A API do GitHub devolveu metadados de asset inválidos.")
        resultado.extend(lote)
        if len(lote) < 100:
            return resultado
    raise RuntimeError("A paginação de assets excedeu o limite defensivo.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fila", type=Path, required=True)
    args = parser.parse_args()
    fila = json.loads(args.fila.read_text(encoding="utf-8"))
    midias = midias_liberadas(fila)
    if not midias:
        print("Nenhum asset liberado para remoção.")
        return

    repositorio = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    tag = os.getenv("RELEASE_TAG", "fila-instagram-facebook")
    _validar_vinculo(fila, repositorio, tag)
    cabecalhos = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    resposta = requests.get(
        f"https://api.github.com/repos/{repositorio}/releases/tags/{tag}",
        headers=cabecalhos,
        timeout=30,
    )
    resposta.raise_for_status()
    release = resposta.json()
    if release.get("tag_name") not in {None, tag}:
        raise RuntimeError("A API do GitHub devolveu uma Release diferente da solicitada.")
    release_id = release.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int):
        raise RuntimeError("A API do GitHub não identificou a Release solicitada.")
    todos_assets = _listar_todos_assets(repositorio, release_id, cabecalhos)
    por_nome: dict[str, dict] = {}
    for asset in todos_assets:
        nome_asset = asset.get("name")
        if not isinstance(nome_asset, str) or not nome_asset:
            raise RuntimeError("A API do GitHub devolveu um asset sem nome.")
        if nome_asset in por_nome:
            raise RuntimeError(f"A Release contém nome de asset duplicado: {nome_asset}")
        por_nome[nome_asset] = asset
    nomes = [str(midia.get("asset") or "") for midia in midias]
    if len(nomes) != len(set(nomes)):
        raise RuntimeError("O mesmo asset foi liberado mais de uma vez na fila.")

    ausentes_recuperaveis: list[dict] = []
    existentes: list[tuple[dict, dict]] = []
    for midia in midias:
        asset = por_nome.get(midia["asset"])
        if not asset:
            if ausencia_autorizada(midia, repositorio, tag):
                ausentes_recuperaveis.append(midia)
                continue
            raise RuntimeError(
                f"Asset {midia['asset']} ausente; remoção não será presumida sem intenção durável."
            )
        _conferir_asset(asset, midia, cabecalhos)
        existentes.append((midia, asset))

    instante = datetime.now(BRT).isoformat()
    for midia, _ in existentes:
        midia.update(
            {
                "remocao_solicitada_em": instante,
                "remocao_repositorio": repositorio,
                "remocao_release_tag": tag,
            }
        )
    if existentes:
        # O tombstone de intenção chega ao remoto antes do primeiro DELETE.
        persistir_fila(args.fila, fila)

    for midia in ausentes_recuperaveis:
        midia["removido_da_release_em"] = datetime.now(BRT).isoformat()
        persistir_fila(args.fila, fila)
    for midia, asset in existentes:
        remocao = requests.delete(
            f"https://api.github.com/repos/{repositorio}/releases/assets/{asset['id']}",
            headers=cabecalhos,
            timeout=30,
        )
        if remocao.status_code not in {204, 404}:
            raise RuntimeError(
                f"Falha ao remover {midia['asset']}: HTTP {remocao.status_code}"
            )
        midia["removido_da_release_em"] = datetime.now(BRT).isoformat()
        persistir_fila(args.fila, fila)


if __name__ == "__main__":
    main()
