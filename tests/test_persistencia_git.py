from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import meta_comum  # noqa: E402


D0 = "2026-09-14"
LEGENDA = "Siga @mentedecifrada"
REPOSITORIO = "dono/mente-decifrada"
TAG = "fila-instagram-facebook"
AGORA = "2026-09-14T05:01:00-03:00"


def _filas_base() -> tuple[dict, dict]:
    reels = json.loads(
        (RAIZ / "fila" / "fila-reels.json").read_text(encoding="utf-8")
    )
    stories = json.loads(
        (RAIZ / "fila" / "fila-stories.json").read_text(encoding="utf-8")
    )
    for fila in (reels, stories):
        fila["github"] = {"repositorio": REPOSITORIO, "release_tag": TAG}
        fila["politica"]["data_inicio_aquecimento"] = D0
        fila["politica"]["reels"]["legenda"] = LEGENDA
    return reels, stories


def _midia(caractere: str) -> dict:
    sha = caractere * 64
    asset = f"sha256-{sha}.mp4"
    return {
        "asset": asset,
        "url_publica": (
            f"https://github.com/{REPOSITORIO}/releases/download/{TAG}/{asset}"
        ),
        "sha256": sha,
        "tamanho_bytes": 1234,
        "duracao_segundos": 30.0,
    }


def _reel(
    data: str,
    caractere: str,
    *,
    instagram: dict | None = None,
    facebook: dict | None = None,
) -> dict:
    estado_instagram = {
        "legenda": LEGENDA,
        "share_to_feed": True,
        "status": "pendente",
    }
    estado_facebook = {"legenda": LEGENDA, "status": "pendente"}
    estado_instagram.update(instagram or {})
    estado_facebook.update(facebook or {})
    return {
        "id": f"reel-{data}-05-00",
        "data": data,
        "horario": "05:00",
        "status": "pendente",
        "aprovado": True,
        "origem": {
            "arquivo": f"{caractere} - reel.mp4",
            "sha256": (caractere * 32) + (("f" if caractere != "f" else "e") * 32),
        },
        "midia": _midia(caractere),
        "instagram": estado_instagram,
        "facebook": estado_facebook,
    }


def _resultado_git(
    argumentos: list[str],
    codigo: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["git", *argumentos], codigo, stdout=stdout, stderr=stderr
    )


class GitConcorrenteFalso:
    def __init__(self, filas_remotas: dict[str, dict]) -> None:
        self.filas_remotas = filas_remotas
        self.chamadas: list[list[str]] = []
        self.pushes = 0
        self.listagens_conflito = 0

    def __call__(
        self, argumentos: list[str], *, conferir: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argumentos = list(argumentos)
        self.chamadas.append(argumentos)
        if argumentos[:2] == ["diff", "--cached"]:
            return _resultado_git(argumentos, 1)
        if argumentos == ["push"]:
            self.pushes += 1
            if self.pushes == 1:
                return _resultado_git(
                    argumentos,
                    1,
                    stderr="! [rejected] main -> main (non-fast-forward)",
                )
            return _resultado_git(argumentos)
        if argumentos == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _resultado_git(argumentos, stdout="main\n")
        if argumentos[:3] == ["fetch", "--no-tags", "origin"]:
            return _resultado_git(argumentos)
        if argumentos and argumentos[0] == "show":
            relativo = argumentos[1].split(":", 1)[1]
            return _resultado_git(
                argumentos,
                stdout=json.dumps(self.filas_remotas[relativo], ensure_ascii=False),
            )
        if argumentos == ["merge", "--no-commit", "--no-ff", "FETCH_HEAD"]:
            return _resultado_git(argumentos, 1, stderr="CONFLICT (content)")
        if argumentos == ["diff", "--name-only", "--diff-filter=U"]:
            self.listagens_conflito += 1
            conflitos = (
                "fila/fila-reels.json\n"
                if self.listagens_conflito == 1
                else ""
            )
            return _resultado_git(argumentos, stdout=conflitos)
        if argumentos[:2] == ["merge", "--abort"]:
            return _resultado_git(argumentos)
        if argumentos and argumentos[0] in {"add", "-c"}:
            return _resultado_git(argumentos)
        raise AssertionError(f"Comando Git inesperado no teste: {argumentos!r}")


class TestMesclaSemantica(unittest.TestCase):
    def test_preserva_estados_e_intencoes_das_duas_execucoes(self) -> None:
        local, _ = _filas_base()
        local["conteudos"] = [
            _reel(
                D0,
                "a",
                instagram={
                    "status": "processando",
                    "fase": "container_criado",
                    "container_id": "container-1",
                },
            )
        ]
        remoto = copy.deepcopy(local)
        remoto["conteudos"][0]["instagram"] = {
            "legenda": LEGENDA,
            "share_to_feed": True,
            "status": "pendente",
        }
        remoto["conteudos"][0]["facebook"].update(
            {
                "status": "enviando",
                "fase": "sessao_criada",
                "video_id": "video-1",
                "upload_url": (
                    "https://rupload.facebook.com/video-upload/v23.0/video-1"
                ),
            }
        )
        remoto["conteudos"].append(_reel("2026-09-15", "b"))

        mesclada = meta_comum.mesclar_filas_semantico(remoto, local)

        principal = mesclada["conteudos"][0]
        self.assertEqual(principal["instagram"]["container_id"], "container-1")
        self.assertEqual(principal["facebook"]["video_id"], "video-1")
        self.assertEqual(principal["facebook"]["status"], "enviando")
        self.assertEqual(len(mesclada["conteudos"]), 2)

    def test_ids_abandonados_nao_sao_ressuscitados_e_historicos_sao_unidos(self) -> None:
        remoto, _ = _filas_base()
        remoto["conteudos"] = [
            _reel(
                D0,
                "a",
                instagram={
                    "status": "processando",
                    "fase": "container_criado",
                    "container_id": "container-morto",
                    "containers_abandonados": [{"id": "container-antigo"}],
                },
                facebook={
                    "status": "processando",
                    "fase": "publicacao_solicitada",
                    "video_id": "video-morto",
                    "upload_url": (
                        "https://rupload.facebook.com/video-upload/v23.0/video-morto"
                    ),
                    "videos_abandonados": [{"id": "video-antigo"}],
                },
            )
        ]
        local = copy.deepcopy(remoto)
        local["conteudos"][0]["instagram"] = {
            "legenda": LEGENDA,
            "share_to_feed": True,
            "status": "erro",
            "erro": "expirado",
            "ultima_tentativa_em": AGORA,
            "fase": "identificador_abandonado",
            "containers_abandonados": [{"id": "container-morto"}],
        }
        local["conteudos"][0]["facebook"] = {
            "legenda": LEGENDA,
            "status": "erro",
            "erro": "falhou",
            "ultima_tentativa_em": AGORA,
            "fase": "identificador_abandonado",
            "videos_abandonados": [{"id": "video-morto"}],
        }

        estado = meta_comum.mesclar_filas_semantico(remoto, local)["conteudos"][0]

        self.assertNotIn("container_id", estado["instagram"])
        self.assertNotIn("video_id", estado["facebook"])
        self.assertNotIn("upload_url", estado["facebook"])
        self.assertEqual(
            {item["id"] for item in estado["instagram"]["containers_abandonados"]},
            {"container-antigo", "container-morto"},
        )
        self.assertEqual(
            {item["id"] for item in estado["facebook"]["videos_abandonados"]},
            {"video-antigo", "video-morto"},
        )

    def test_fase_e_finish_avancam_sem_regressao(self) -> None:
        remoto, _ = _filas_base()
        remoto["conteudos"] = [
            _reel(
                D0,
                "a",
                facebook={
                    "status": "processando",
                    "fase": "sessao_criada",
                    "video_id": "video-1",
                    "upload_url": (
                        "https://rupload.facebook.com/video-upload/v23.0/video-1"
                    ),
                    "finish_solicitado_em": "2026-09-14T05:02:00-03:00",
                },
            )
        ]
        local = copy.deepcopy(remoto)
        local["conteudos"][0]["facebook"].update(
            {
                "fase": "publicacao_solicitada",
                "finish_solicitado_em": "2026-09-14T05:03:00-03:00",
            }
        )

        facebook = meta_comum.mesclar_filas_semantico(remoto, local)["conteudos"][0][
            "facebook"
        ]

        self.assertEqual(facebook["fase"], "publicacao_solicitada")
        self.assertEqual(
            facebook["finish_solicitado_em"], "2026-09-14T05:03:00-03:00"
        )

    def test_alerta_sem_media_id_e_fase_conhecida_e_mais_avancada(self) -> None:
        remoto, _ = _filas_base()
        remoto["conteudos"] = [
            _reel(
                D0,
                "a",
                instagram={
                    "status": "processando",
                    "fase": "publicacao_solicitada",
                    "container_id": "container-1",
                },
            )
        ]
        local = copy.deepcopy(remoto)
        local["conteudos"][0]["instagram"].update(
            {
                "fase": "publicado_sem_media_id",
                "reconciliacao_manual": {
                    "necessaria": True,
                    "motivo": "resposta ambígua",
                    "detectada_em": AGORA,
                },
            }
        )

        instagram = meta_comum.mesclar_filas_semantico(remoto, local)["conteudos"][0][
            "instagram"
        ]

        self.assertEqual(instagram["fase"], "publicado_sem_media_id")
        self.assertTrue(instagram["reconciliacao_manual"]["necessaria"])

    def test_publicado_com_id_real_descarta_alerta_manual_nos_dois_sentidos(self) -> None:
        base, _ = _filas_base()
        publicado = _reel(
            D0,
            "a",
            instagram={
                "status": "publicado",
                "fase": "publicacao_confirmada",
                "container_id": "container-1",
                "id": "media-1",
                "publicado_em": AGORA,
                "confirmacao": "media_publish",
            },
        )
        manual = copy.deepcopy(publicado)
        manual["instagram"] = {
            "legenda": LEGENDA,
            "share_to_feed": True,
            "status": "processando",
            "fase": "publicado_sem_media_id",
            "container_id": "container-1",
            "reconciliacao_manual": {
                "necessaria": True,
                "motivo": "resposta ambígua",
                "detectada_em": AGORA,
            },
        }
        for remoto_item, local_item in ((publicado, manual), (manual, publicado)):
            remoto = copy.deepcopy(base)
            local = copy.deepcopy(base)
            remoto["conteudos"] = [copy.deepcopy(remoto_item)]
            local["conteudos"] = [copy.deepcopy(local_item)]

            instagram = meta_comum.mesclar_filas_semantico(remoto, local)["conteudos"][
                0
            ]["instagram"]

            self.assertEqual(instagram["status"], "publicado")
            self.assertEqual(instagram["id"], "media-1")
            self.assertEqual(instagram["fase"], "publicacao_confirmada")
            self.assertNotIn("reconciliacao_manual", instagram)

    def test_ids_ativos_divergentes_nao_sao_sobrescritos(self) -> None:
        remoto, _ = _filas_base()
        remoto["conteudos"] = [
            _reel(
                D0,
                "a",
                instagram={"status": "processando", "container_id": "remoto"},
            )
        ]
        local = copy.deepcopy(remoto)
        local["conteudos"][0]["instagram"]["container_id"] = "local"

        with self.assertRaisesRegex(RuntimeError, "não será sobrescrito"):
            meta_comum.mesclar_filas_semantico(remoto, local)


class TestRetryGit(unittest.TestCase):
    def test_conflito_e_checkpoint_sequencial_preservam_item_remoto(self) -> None:
        local, stories = _filas_base()
        local["conteudos"] = [
            _reel(
                D0,
                "a",
                instagram={
                    "status": "processando",
                    "fase": "container_criado",
                    "container_id": "container-1",
                },
            )
        ]
        remoto = copy.deepcopy(local)
        remoto["conteudos"][0]["instagram"] = {
            "legenda": LEGENDA,
            "share_to_feed": True,
            "status": "pendente",
        }
        remoto["conteudos"][0]["facebook"].update(
            {
                "status": "enviando",
                "fase": "sessao_criada",
                "video_id": "video-1",
                "upload_url": (
                    "https://rupload.facebook.com/video-upload/v23.0/video-1"
                ),
            }
        )
        remoto["conteudos"].append(_reel("2026-09-15", "b"))
        git = GitConcorrenteFalso(
            {
                "fila/fila-reels.json": remoto,
                "fila/fila-stories.json": stories,
            }
        )
        conteudos = local["conteudos"]
        item = conteudos[0]
        estado_instagram = item["instagram"]
        estado_facebook = item["facebook"]

        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            caminho = raiz / "fila" / "fila-reels.json"
            caminho.parent.mkdir()
            with (
                patch.object(meta_comum, "ROOT", raiz),
                patch.object(meta_comum, "_executar_git", side_effect=git),
                patch.dict(
                    os.environ,
                    {
                        "PERSISTIR_ESTADO_REMOTO": "true",
                        "DATA_PUBLICACAO": D0,
                        "HORARIO_PUBLICACAO": "05:00",
                    },
                    clear=False,
                ),
            ):
                meta_comum.persistir_fila(caminho, local)
                self.assertEqual(len(local["conteudos"]), 2)
                self.assertIs(local["conteudos"], conteudos)
                self.assertIs(local["conteudos"][0], item)
                self.assertIs(local["conteudos"][0]["instagram"], estado_instagram)
                self.assertIs(local["conteudos"][0]["facebook"], estado_facebook)
                self.assertEqual(estado_facebook["video_id"], "video-1")
                estado_instagram["fase"] = "publicacao_solicitada"
                meta_comum.persistir_fila(caminho, local)

            persistida = json.loads(caminho.read_text(encoding="utf-8"))

        self.assertEqual(len(persistida["conteudos"]), 2)
        self.assertEqual(persistida["conteudos"][1]["id"], "reel-2026-09-15-05-00")
        self.assertEqual(
            persistida["conteudos"][0]["instagram"]["fase"],
            "publicacao_solicitada",
        )
        self.assertEqual(git.pushes, 3)
        self.assertTrue(any(chamada[0] == "fetch" for chamada in git.chamadas))
        self.assertFalse(
            any("--force" in argumento for chamada in git.chamadas for argumento in chamada)
        )

    def test_atualizacao_profunda_preserva_pacote_parte_e_plataforma(self) -> None:
        fila = {
            "pacotes": [
                {
                    "id": "story-1",
                    "data": D0,
                    "horario": "09:00",
                    "partes": [
                        {
                            "ordem": 1,
                            "instagram": {"status": "processando"},
                            "facebook": {"status": "pendente"},
                        }
                    ],
                }
            ]
        }
        autoritativa = copy.deepcopy(fila)
        autoritativa["pacotes"][0]["partes"][0]["instagram"]["fase"] = (
            "publicacao_solicitada"
        )
        autoritativa["pacotes"].append(
            {
                "id": "story-2",
                "data": "2026-09-15",
                "horario": "09:00",
                "partes": [],
            }
        )
        pacotes = fila["pacotes"]
        pacote = pacotes[0]
        partes = pacote["partes"]
        parte = partes[0]
        estado = parte["instagram"]

        meta_comum._atualizar_mapeamento_inplace(fila, autoritativa)

        self.assertIs(fila["pacotes"], pacotes)
        self.assertIs(fila["pacotes"][0], pacote)
        self.assertIs(fila["pacotes"][0]["partes"], partes)
        self.assertIs(fila["pacotes"][0]["partes"][0], parte)
        self.assertIs(fila["pacotes"][0]["partes"][0]["instagram"], estado)
        self.assertEqual(estado["fase"], "publicacao_solicitada")
        self.assertEqual(len(fila["pacotes"]), 2)

    def test_mescla_invalida_e_bloqueada_antes_do_merge_git(self) -> None:
        local, stories = _filas_base()
        local["conteudos"] = [_reel("2026-09-15", "a")]
        remoto = copy.deepcopy(local)
        remoto["conteudos"] = [_reel(D0, "a")]
        git = GitConcorrenteFalso(
            {
                "fila/fila-reels.json": remoto,
                "fila/fila-stories.json": stories,
            }
        )

        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            caminho = raiz / "fila" / "fila-reels.json"
            caminho.parent.mkdir()
            caminho.write_text(json.dumps(local), encoding="utf-8")
            with patch.object(meta_comum, "ROOT", raiz), self.assertRaisesRegex(
                RuntimeError, "filas inválidas"
            ):
                meta_comum.persistir_arquivos_git([caminho], "teste", executor=git)

        self.assertFalse(
            any(chamada[:2] == ["merge", "--no-commit"] for chamada in git.chamadas)
        )


class TestWorkflows(unittest.TestCase):
    def test_workflows_usam_persistencia_segura_sem_force_push(self) -> None:
        reels = (RAIZ / ".github" / "workflows" / "publicar-reels.yml").read_text(
            encoding="utf-8"
        )
        stories = (
            RAIZ / ".github" / "workflows" / "publicar-stories.yml"
        ).read_text(encoding="utf-8")
        for conteudo in (reels, stories):
            self.assertIn("queue: max", conteudo)
            self.assertIn("python meta_comum.py persistir-git", conteudo)
            self.assertNotIn("git push", conteudo)
            self.assertNotIn("--force", conteudo)
        self.assertIn("  publicar_stories:", stories)
        self.assertIn("  publicar_reel_09:", stories)
        self.assertNotIn("needs:", stories)


if __name__ == "__main__":
    unittest.main()
