from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import publicar_reels  # noqa: E402
import publicar_stories  # noqa: E402
import meta_comum  # noqa: E402
import limpar_release  # noqa: E402
from limpar_release import (  # noqa: E402
    _conferir_asset,
    _listar_todos_assets,
    ausencia_autorizada,
    midias_liberadas,
)
from meta_comum import alvo_exato, validar_upload_url_meta, validar_url_midia  # noqa: E402


class TestSelecaoExata(unittest.TestCase):
    def setUp(self) -> None:
        self.itens = [
            {"data": "2026-09-14", "horario": "05:00", "status": "pendente"},
            {"data": "2026-09-14", "horario": "09:00", "status": "pendente"},
            {"data": "2026-09-13", "horario": "21:00", "status": "pendente"},
        ]

    def test_nao_consume_atrasado_de_outro_slot(self) -> None:
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-14", "HORARIO_PUBLICACAO": "09:00"},
            clear=False,
        ):
            self.assertIs(alvo_exato(self.itens), self.itens[1])

    def test_slot_ausente_nao_publica(self) -> None:
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-14", "HORARIO_PUBLICACAO": "13:00"},
            clear=False,
        ):
            self.assertIsNone(alvo_exato(self.itens))

    def test_rejeita_duplicidade_no_mesmo_slot(self) -> None:
        duplicados = [self.itens[0], dict(self.itens[0])]
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-14", "HORARIO_PUBLICACAO": "05:00"},
            clear=False,
        ), self.assertRaisesRegex(RuntimeError, "Mais de um"):
            alvo_exato(duplicados)


class TestLimpezaSegura(unittest.TestCase):
    def test_so_libera_quando_as_duas_redes_confirmam(self) -> None:
        fila = {
            "conteudos": [
                {
                    "midia": {"asset": "confirmado.mp4"},
                    "instagram": {"status": "publicado"},
                    "facebook": {"status": "publicado"},
                },
                {
                    "midia": {"asset": "parcial.mp4"},
                    "instagram": {"status": "publicado"},
                    "facebook": {"status": "erro"},
                },
            ]
        }
        self.assertEqual(
            [midia["asset"] for midia in midias_liberadas(fila)],
            ["confirmado.mp4"],
        )

    def test_parte_de_story_confirmada_pode_ser_liberada_individualmente(self) -> None:
        fila = {
            "pacotes": [
                {
                    "partes": [
                        {
                            "midia": {"asset": "parte-01.mp4"},
                            "instagram": {"status": "publicado"},
                            "facebook": {"status": "publicado"},
                        },
                        {
                            "midia": {"asset": "parte-02.mp4"},
                            "instagram": {"status": "pendente"},
                            "facebook": {"status": "pendente"},
                        },
                    ]
                }
            ]
        }
        self.assertEqual(
            [midia["asset"] for midia in midias_liberadas(fila)],
            ["parte-01.mp4"],
        )

    def test_confere_digest_e_tamanho_antes_de_excluir(self) -> None:
        digest = "a" * 64
        _conferir_asset(
            {"name": "sha256-a.mp4", "size": 12, "digest": f"sha256:{digest}"},
            {"asset": "sha256-a.mp4", "tamanho_bytes": 12, "sha256": digest},
            {},
        )
        with self.assertRaisesRegex(RuntimeError, "SHA-256 divergente"):
            _conferir_asset(
                {"name": "sha256-a.mp4", "size": 12, "digest": "sha256:" + "b" * 64},
                {"asset": "sha256-a.mp4", "tamanho_bytes": 12, "sha256": digest},
                {},
            )

    def test_asset_ausente_so_e_recuperavel_com_intencao_do_mesmo_destino(self) -> None:
        midia = {
            "remocao_solicitada_em": "2026-09-08T10:00:00-03:00",
            "remocao_repositorio": "dono/repo",
            "remocao_release_tag": "fila-instagram-facebook",
        }
        self.assertTrue(ausencia_autorizada(midia, "dono/repo", "fila-instagram-facebook"))
        self.assertFalse(ausencia_autorizada(midia, "outro/repo", "fila-instagram-facebook"))
        self.assertFalse(ausencia_autorizada({}, "dono/repo", "fila-instagram-facebook"))

    def test_lista_todas_as_paginas_da_release(self) -> None:
        primeira = MagicMock()
        primeira.json.return_value = [{"name": f"asset-{i}"} for i in range(100)]
        segunda = MagicMock()
        segunda.json.return_value = [{"name": "asset-100"}]
        with patch.object(
            limpar_release.requests, "get", side_effect=[primeira, segunda]
        ) as obter:
            assets = _listar_todos_assets("dono/repo", 7, {})
        self.assertEqual(len(assets), 101)
        self.assertEqual(obter.call_count, 2)
        self.assertEqual(obter.call_args_list[1].kwargs["params"]["page"], 2)


class TestAmarracaoDaMidia(unittest.TestCase):
    def test_url_precisa_corresponder_ao_repo_tag_e_asset(self) -> None:
        midia = {
            "asset": "sha256-abc.mp4",
            "url_publica": (
                "https://github.com/dono/repo/releases/download/"
                "fila-instagram-facebook/sha256-abc.mp4"
            ),
        }
        with patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": "dono/repo", "RELEASE_TAG": "fila-instagram-facebook"},
            clear=False,
        ):
            validar_url_midia(midia)
            adulterada = dict(midia, url_publica=midia["url_publica"].replace("dono/repo", "outro/repo"))
            with self.assertRaisesRegex(RuntimeError, "não corresponde"):
                validar_url_midia(adulterada)

    def test_url_de_upload_precisa_ser_rupload_e_path_canonico(self) -> None:
        video_id = "video-456"
        canonica = f"https://rupload.facebook.com/video-upload/v23.0/{video_id}"
        self.assertEqual(validar_upload_url_meta(canonica, video_id), canonica)
        for invalida in (
            f"https://graph.facebook.com/video-upload/v23.0/{video_id}",
            f"https://www.facebook.com/video-upload/v23.0/{video_id}",
            f"https://rupload.facebook.com/outra-rota/v23.0/{video_id}",
            f"https://rupload.facebook.com/video-upload/v23.0/outro-id",
        ):
            with self.subTest(invalida=invalida), self.assertRaisesRegex(
                RuntimeError, "URL de upload inesperada"
            ):
                validar_upload_url_meta(invalida, video_id)


class TestRetomadaMeta(unittest.TestCase):
    def test_excecao_de_rede_nunca_expoe_token_na_fila(self) -> None:
        segredo = "token-ultrassecreto-123"
        with patch.object(
            meta_comum.requests,
            "get",
            side_effect=meta_comum.requests.ConnectionError(
                f"falha em https://graph.facebook.com/v23.0/id?access_token={segredo}"
            ),
        ), self.assertRaises(RuntimeError) as capturada:
            meta_comum.graph_get("id", {"access_token": segredo})
        self.assertNotIn(segredo, str(capturada.exception))
        self.assertIn("[REDACTED]", str(capturada.exception))

        estado = {"status": "pendente"}
        item = {"facebook": estado}
        with patch.dict(os.environ, {"FB_PAGE_ACCESS_TOKEN": segredo}, clear=False):
            meta_comum.executar_plataforma(
                item,
                "facebook",
                lambda _item, _estado: (_ for _ in ()).throw(
                    RuntimeError(f"endpoint?access_token={segredo}")
                ),
                lambda: None,
            )
        self.assertNotIn(segredo, estado["erro"])
        self.assertIn("[REDACTED]", estado["erro"])

    def test_instagram_container_publicado_sem_media_id_bloqueia_limpeza(self) -> None:
        estado = {"container_id": "cont-123", "status": "processando", "legenda": "Siga @teste"}
        with (
            patch.object(publicar_reels, "obrigatoria", side_effect=lambda nome: "token" if nome == "IG_ACCESS_TOKEN" else "ig"),
            patch.object(publicar_reels, "verificar_midia_remota") as verificar,
            patch.object(publicar_reels, "aguardar_instagram", return_value="PUBLISHED"),
            patch.object(publicar_reels, "graph_post") as publicar,
            self.assertRaisesRegex(RuntimeError, "ID do objeto Reel"),
        ):
            publicar_reels.publicar_instagram({"midia": {}}, estado, lambda: None)
        verificar.assert_not_called()
        publicar.assert_not_called()
        self.assertTrue(estado["reconciliacao_manual"]["necessaria"])
        self.assertEqual(estado["fase"], "publicado_sem_media_id")

    def test_instagram_container_publicado_reusa_media_id_real_persistido(self) -> None:
        estado = {
            "container_id": "cont-123",
            "id": "media-789",
            "status": "processando",
            "legenda": "Siga @teste",
        }
        with (
            patch.object(publicar_reels, "obrigatoria", return_value="token"),
            patch.object(publicar_reels, "verificar_midia_remota") as verificar,
            patch.object(publicar_reels, "aguardar_instagram", return_value="PUBLISHED"),
        ):
            identificador = publicar_reels.publicar_instagram(
                {"midia": {}}, estado, lambda: None
            )
        self.assertEqual(identificador, "media-789")
        self.assertEqual(estado["status"], "publicado")
        verificar.assert_not_called()

    def test_facebook_reutiliza_video_id_persistido(self) -> None:
        estado = {
            "video_id": "video-456",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-456",
            "status": "erro",
            "legenda": "Siga @teste",
        }
        resposta_fim = {"success": True}
        with tempfile.TemporaryDirectory() as pasta:
            midia = Path(pasta) / "video.mp4"
            midia.write_bytes(b"teste")
            with (
                patch.object(publicar_reels, "token_pagina", return_value=("pagina", "token")),
                patch.object(publicar_reels, "baixar_midia", return_value=midia),
                patch.object(
                    publicar_reels,
                    "consultar_video_facebook",
                    return_value={"status": {"uploading_phase": {"status": "complete"}}},
                ),
                patch.object(publicar_reels, "graph_post", return_value=resposta_fim) as graph_post,
                patch.object(publicar_reels, "aguardar_facebook_publicado"),
                patch.object(publicar_reels.requests, "post") as upload,
            ):
                identificador = publicar_reels.publicar_facebook(
                    {"midia": {}}, estado, lambda: None
                )
        self.assertEqual(identificador, "video-456")
        self.assertEqual(estado["status"], "publicado")
        upload.assert_not_called()
        self.assertEqual(graph_post.call_count, 1)
        self.assertEqual(graph_post.call_args.args[0], "pagina/video_reels")
        self.assertEqual(graph_post.call_args.args[1]["upload_phase"], "finish")

    def test_facebook_reel_reconcilia_antes_de_baixar_release(self) -> None:
        estado = {
            "video_id": "video-456",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-456",
            "status": "processando",
            "legenda": "Siga @teste",
        }
        with (
            patch.object(publicar_reels, "token_pagina", return_value=("pagina", "token")),
            patch.object(publicar_reels, "baixar_midia") as baixar,
            patch.object(
                publicar_reels,
                "consultar_video_facebook",
                return_value={
                    "published": True,
                    "status": {"processing_phase": {"status": "error"}},
                },
            ),
        ):
            identificador = publicar_reels.publicar_facebook(
                {"midia": {}}, estado, lambda: None
            )
        self.assertEqual(identificador, "video-456")
        baixar.assert_not_called()
        self.assertNotIn("videos_abandonados", estado)

    def test_facebook_reel_timeout_prioriza_publicado_em_resposta_contraditoria(self) -> None:
        estado = {
            "video_id": "video-456",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-456",
            "status": "processando",
            "legenda": "Siga @teste",
        }
        with (
            patch.object(
                publicar_reels,
                "aguardar_facebook_publicado",
                side_effect=TimeoutError("resposta final ambigua"),
            ),
            patch.object(
                publicar_reels,
                "consultar_video_facebook",
                return_value={
                    "published": True,
                    "status": {"processing_phase": {"status": "error"}},
                },
            ),
        ):
            publicar_reels._aguardar_facebook_seguro(
                "video-456", "token", estado, lambda: None
            )
        self.assertEqual(estado["video_id"], "video-456")
        self.assertNotIn("videos_abandonados", estado)

    def test_story_facebook_reconcilia_post_id_na_aresta_stories(self) -> None:
        estado = {
            "video_id": "video-456",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-456",
            "status": "processando",
            "fase": "publicacao_solicitada",
        }
        with (
            patch.object(publicar_stories, "token_pagina", return_value=("pagina", "token")),
            patch.object(publicar_stories, "baixar_midia") as baixar,
            patch.object(
                publicar_stories,
                "consultar_video_facebook",
                return_value={
                    "published": True,
                    "status": {"processing_phase": {"status": "error"}},
                },
            ),
            patch.object(
                publicar_stories,
                "graph_get",
                return_value={
                    "data": [
                        {
                            "id": "story-objeto",
                            "post_id": "post-789",
                            "media_id": "video-456",
                            "status": "PUBLISHED",
                        }
                    ]
                },
            ),
        ):
            identificador = publicar_stories.publicar_facebook(
                {"midia": {}}, estado, lambda: None
            )
        self.assertEqual(identificador, "post-789")
        self.assertEqual(estado["id"], "post-789")
        self.assertNotEqual(estado["id"], estado["video_id"])
        baixar.assert_not_called()
        self.assertNotIn("videos_abandonados", estado)

    def test_story_facebook_nunca_promove_video_id_sem_comprovar_story(self) -> None:
        estado = {
            "video_id": "video-456",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-456",
            "status": "processando",
            "fase": "publicacao_solicitada",
        }
        with (
            patch.object(publicar_stories, "token_pagina", return_value=("pagina", "token")),
            patch.object(publicar_stories, "baixar_midia") as baixar,
            patch.object(
                publicar_stories,
                "consultar_video_facebook",
                return_value={
                    "published": True,
                    "status": {"processing_phase": {"status": "error"}},
                },
            ),
            patch.object(publicar_stories, "graph_get", return_value={"data": []}),
            self.assertRaisesRegex(RuntimeError, "não foi comprovado"),
        ):
            publicar_stories.publicar_facebook({"midia": {}}, estado, lambda: None)
        self.assertNotEqual(estado.get("status"), "publicado")
        self.assertNotIn("id", estado)
        self.assertEqual(estado["video_id"], "video-456")
        self.assertNotIn("videos_abandonados", estado)
        baixar.assert_not_called()

    def test_reconciliacao_story_percorre_paginacao_por_cursor(self) -> None:
        primeira = {
            "data": [{"media_id": "outro", "status": "PUBLISHED"}],
            "paging": {"cursors": {"after": "cursor-seguro"}},
        }
        segunda = {
            "data": [
                {
                    "id": "story-objeto",
                    "post_id": "post-789",
                    "media_id": "video-456",
                    "status": "ARCHIVED",
                }
            ]
        }
        with patch.object(
            publicar_stories, "graph_get", side_effect=[primeira, segunda]
        ) as obter:
            post_id = publicar_stories._reconciliar_story_facebook(
                "pagina", "video-456", "token"
            )
        self.assertEqual(post_id, "post-789")
        self.assertEqual(obter.call_count, 2)
        self.assertEqual(obter.call_args_list[1].args[1]["after"], "cursor-seguro")

    def test_upload_facebook_exige_success_true_explicito(self) -> None:
        estado = {
            "video_id": "video-456",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-456",
            "status": "enviando",
            "legenda": "Siga @teste",
        }
        resposta = MagicMock(ok=True)
        resposta.json.return_value = {}
        with tempfile.TemporaryDirectory() as pasta:
            midia = Path(pasta) / "video.mp4"
            midia.write_bytes(b"teste")
            with (
                patch.object(publicar_reels, "token_pagina", return_value=("pagina", "token")),
                patch.object(publicar_reels, "baixar_midia", return_value=midia),
                patch.object(
                    publicar_reels,
                    "consultar_video_facebook",
                    return_value={
                        "status": {"uploading_phase": {"status": "not_started"}}
                    },
                ),
                patch.object(publicar_reels.requests, "post", return_value=resposta),
                patch.object(publicar_reels, "graph_post") as finalizar,
                self.assertRaisesRegex(RuntimeError, "não confirmou o upload"),
            ):
                publicar_reels.publicar_facebook({"midia": {}}, estado, lambda: None)
        finalizar.assert_not_called()

    def test_facebook_abandona_apenas_id_com_falha_conclusiva(self) -> None:
        estado = {
            "video_id": "video-morto",
            "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-morto",
            "status": "erro",
            "legenda": "Siga @teste",
        }
        persistencias: list[str] = []
        with (
            patch.object(publicar_reels, "token_pagina", return_value=("pagina", "token")),
            patch.object(
                publicar_reels,
                "consultar_video_facebook",
                return_value={"status": {"video_status": "failed"}},
            ),
            patch.object(
                publicar_reels,
                "baixar_midia",
                side_effect=RuntimeError("sem mídia para a nova sessão"),
            ),
            self.assertRaisesRegex(RuntimeError, "sem mídia"),
        ):
            publicar_reels.publicar_facebook(
                {"midia": {}}, estado, lambda: persistencias.append(estado["fase"])
            )
        self.assertNotIn("video_id", estado)
        self.assertNotIn("upload_url", estado)
        self.assertEqual(estado["videos_abandonados"][0]["id"], "video-morto")
        self.assertTrue(estado["erro"])
        self.assertTrue(estado["ultima_tentativa_em"])
        self.assertIn("identificador_abandonado", persistencias)

    def test_instagram_abandona_container_expirado_antes_de_criar_outro(self) -> None:
        estado = {
            "container_id": "container-morto",
            "status": "erro",
            "legenda": "Siga @teste",
        }
        with (
            patch.object(publicar_reels, "obrigatoria", return_value="token"),
            patch.object(
                publicar_reels,
                "aguardar_instagram",
                side_effect=RuntimeError("container expirou"),
            ),
            patch.object(
                publicar_reels,
                "consultar_container_instagram",
                return_value={"status_code": "EXPIRED"},
            ),
            patch.object(
                publicar_reels,
                "verificar_midia_remota",
                side_effect=RuntimeError("sem mídia para o novo container"),
            ),
            self.assertRaisesRegex(RuntimeError, "sem mídia"),
        ):
            publicar_reels.publicar_instagram({"midia": {}}, estado, lambda: None)
        self.assertNotIn("container_id", estado)
        self.assertEqual(
            estado["containers_abandonados"][0]["id"], "container-morto"
        )
        self.assertTrue(estado["erro"])
        self.assertTrue(estado["ultima_tentativa_em"])

    def test_persistencia_adota_estado_autoritativo_depois_do_push(self) -> None:
        autoritativa = {"canal": "instagram-facebook-reels", "conteudos": []}
        dados: dict = {}
        with (
            patch.dict(os.environ, {"PERSISTIR_ESTADO_REMOTO": "true"}, clear=False),
            patch.object(meta_comum, "salvar_json_atomico"),
            patch.object(
                meta_comum,
                "persistir_arquivos_git",
                return_value={"fila/teste.json": autoritativa},
            ) as persistir_git,
        ):
            meta_comum.persistir_fila(
                meta_comum.ROOT / "fila" / "teste.json", dados
            )
        persistir_git.assert_called_once()
        self.assertEqual(dados, autoritativa)


if __name__ == "__main__":
    unittest.main()
