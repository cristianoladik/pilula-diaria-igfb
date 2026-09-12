from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditar_release import (  # noqa: E402
    AssetRelease,
    ReferenciaAsset,
    auditar_assets,
    coletar_assets_referenciados,
    consultar_assets_release,
    linhas_relatorio,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def referencia(
    nome: str = "video.mp4",
    *,
    sha256: str = SHA_A,
    tamanho: int = 123,
    pendente: bool = True,
) -> ReferenciaAsset:
    return ReferenciaAsset(nome, sha256, tamanho, "Reel teste", pendente)


class TestColetaDasFilas(unittest.TestCase):
    def test_coleta_somente_a_pendencia_real_de_cada_unidade(self) -> None:
        reels = {
            "conteudos": [
                {
                    "id": "reel-pendente",
                    "status": "pendente",
                    "midia": {
                        "asset": "reel.mp4",
                        "sha256": SHA_A,
                        "tamanho_bytes": 10,
                    },
                    "instagram": {"status": "pendente"},
                    "facebook": {"status": "pendente"},
                },
                {
                    "id": "reel-concluido",
                    "status": "concluido",
                    "midia": {
                        "asset": "antigo.mp4",
                        "sha256": SHA_B,
                        "tamanho_bytes": 20,
                    },
                },
            ]
        }
        stories = {
            "pacotes": [
                {
                    "id": "story-parcial",
                    "status": "pendente",
                    "partes": [
                        {
                            "ordem": 1,
                            "midia": {
                                "asset": "parte-publicada.mp4",
                                "sha256": SHA_A,
                                "tamanho_bytes": 30,
                            },
                            "instagram": {"status": "publicado"},
                            "facebook": {"status": "publicado"},
                        },
                        {
                            "ordem": 2,
                            "midia": {
                                "asset": "parte-pendente.mp4",
                                "sha256": SHA_B,
                                "tamanho_bytes": 40,
                            },
                            "instagram": {"status": "erro"},
                            "facebook": {"status": "pendente"},
                        },
                    ],
                }
            ]
        }

        coletadas = coletar_assets_referenciados(reels, stories)

        self.assertEqual(len(coletadas), 4)
        self.assertEqual(
            [item.nome for item in coletadas if item.pendente],
            ["reel.mp4", "parte-pendente.mp4"],
        )


class TestAuditoriaPura(unittest.TestCase):
    def test_asset_integro_passa(self) -> None:
        resultado = auditar_assets(
            [referencia()],
            [AssetRelease("video.mp4", SHA_A, 123)],
        )

        self.assertFalse(resultado.falhou)
        self.assertEqual(resultado.bytes_release, 123)
        self.assertEqual(resultado.bytes_pendentes, 123)

    def test_asset_pendente_ausente_falha(self) -> None:
        resultado = auditar_assets([referencia()], [])

        self.assertTrue(resultado.falhou)
        self.assertEqual([item.nome for item in resultado.ausentes], ["video.mp4"])

    def test_tamanho_e_hash_divergentes_falham(self) -> None:
        resultado = auditar_assets(
            [referencia()],
            [AssetRelease("video.mp4", SHA_B, 999)],
        )

        self.assertTrue(resultado.falhou)
        self.assertEqual(len(resultado.divergentes), 1)
        self.assertIn("tamanho divergente", resultado.divergentes[0].motivo)
        self.assertIn("SHA-256 divergente", resultado.divergentes[0].motivo)

    def test_asset_concluido_ausente_nao_falha(self) -> None:
        resultado = auditar_assets([referencia(pendente=False)], [])

        self.assertFalse(resultado.falhou)
        self.assertFalse(resultado.ausentes)

    def test_orfao_e_apenas_aviso_e_bytes_sao_relacionados(self) -> None:
        resultado = auditar_assets(
            [referencia()],
            [
                AssetRelease("video.mp4", SHA_A, 123),
                AssetRelease("orfao.mp4", SHA_B, 2048),
            ],
        )

        self.assertFalse(resultado.falhou)
        self.assertEqual([item.nome for item in resultado.orfaos], ["orfao.mp4"])
        self.assertEqual(resultado.bytes_orfaos, 2048)
        self.assertTrue(any("nenhuma remoção" in linha for linha in linhas_relatorio(resultado)))

    def test_metadados_invalidos_da_fila_sao_divergencia(self) -> None:
        resultado = auditar_assets(
            [referencia(sha256="invalido", tamanho=0)],
            [AssetRelease("video.mp4", SHA_A, 123)],
        )

        self.assertTrue(resultado.falhou)
        self.assertIn("tamanho inválido", resultado.divergentes[0].motivo)
        self.assertIn("SHA-256 inválido", resultado.divergentes[0].motivo)


class RespostaFalsa:
    def __init__(self, dados) -> None:
        self._dados = dados

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._dados


class TestConsultaSemRede(unittest.TestCase):
    def test_usa_somente_get_autenticado_e_normaliza_digest(self) -> None:
        chamadas: list[tuple[str, dict, dict | None, int]] = []

        def get_falso(
            url: str, *, headers: dict, timeout: int, params: dict | None = None
        ) -> RespostaFalsa:
            chamadas.append((url, headers, params, timeout))
            if "/releases/tags/" in url:
                return RespostaFalsa({"id": 7})
            return RespostaFalsa(
                [{"name": "video.mp4", "size": 123, "digest": f"sha256:{SHA_A}"}]
            )

        assets = consultar_assets_release(
            "dono/repositorio",
            "fila com espaço",
            "token-de-teste",
            http_get=get_falso,
        )

        self.assertEqual(assets, (AssetRelease("video.mp4", SHA_A, 123),))
        self.assertEqual(len(chamadas), 2)
        self.assertTrue(chamadas[0][0].endswith("/releases/tags/fila%20com%20espa%C3%A7o"))
        self.assertEqual(chamadas[0][1]["Authorization"], "Bearer token-de-teste")
        self.assertIsNone(chamadas[0][2])
        self.assertEqual(chamadas[1][2], {"per_page": 100, "page": 1})
        self.assertEqual(chamadas[0][3], 30)


if __name__ == "__main__":
    unittest.main()
