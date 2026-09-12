from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import quote


RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from validar_filas import validar_filas  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
REPOSITORIO = "dono/mente-decifrada"
TAG = "fila-instagram-facebook"
D0 = "2026-09-14"
LEGENDA = "Siga @mentedecifrada"


def carregar_bases() -> tuple[dict, dict]:
    reels = json.loads((RAIZ / "fila" / "fila-reels.json").read_text(encoding="utf-8"))
    stories = json.loads((RAIZ / "fila" / "fila-stories.json").read_text(encoding="utf-8"))
    return reels, stories


def operacionalizar(reels: dict, stories: dict) -> None:
    for fila in (reels, stories):
        fila["github"]["repositorio"] = REPOSITORIO
        fila["github"]["release_tag"] = TAG
        fila["politica"]["data_inicio_aquecimento"] = D0
        fila["politica"]["reels"]["legenda"] = LEGENDA


def midia(sha256: str, duracao: float) -> dict:
    asset = f"sha256-{sha256}.mp4"
    return {
        "asset": asset,
        "url_publica": (
            f"https://github.com/{REPOSITORIO}/releases/download/"
            f"{quote(TAG, safe='')}/{quote(asset, safe='')}"
        ),
        "sha256": sha256,
        "tamanho_bytes": 1234,
        "duracao_segundos": duracao,
    }


def reel(*, data: str = D0, horario: str = "05:00", duracao: float = 60.0) -> dict:
    return {
        "id": f"reel-{data}-{horario}",
        "data": data,
        "horario": horario,
        "status": "pendente",
        "aprovado": True,
        "origem": {"arquivo": "001 - reel.mp4", "sha256": SHA_A},
        "midia": midia(SHA_A, duracao),
        "instagram": {
            "legenda": LEGENDA,
            "share_to_feed": True,
            "status": "pendente",
        },
        "facebook": {"legenda": LEGENDA, "status": "pendente"},
    }


def story(*, duracao: float = 59.0) -> dict:
    return {
        "id": "story-20260914",
        "data": D0,
        "horario": "09:00",
        "status": "pendente",
        "aprovado": True,
        "origem": {"arquivo": "001 - story.mp4", "sha256": SHA_C},
        "partes": [
            {
                "ordem": 1,
                "midia": midia(SHA_B, duracao),
                "instagram": {"status": "pendente"},
                "facebook": {"status": "pendente"},
            }
        ],
    }


class TestFilasBase(unittest.TestCase):
    def test_bases_do_repositorio_validam(self) -> None:
        # Ate 10/09/2026 as filas tinham placeholders "__CONFIGURAR__" e o teste provava
        # que item com placeholder era recusado. A politica foi preenchida com os dados
        # reais do projeto, entao o que se verifica agora e que as bases validam limpas.
        reels, stories = carregar_bases()
        self.assertEqual(validar_filas(reels, stories), [])

    def test_placeholder_continua_recusando_item(self) -> None:
        reels, stories = carregar_bases()
        reels["politica"]["data_inicio_aquecimento"] = "__CONFIGURAR__"
        reels["conteudos"].append(reel())
        erros = validar_filas(reels, stories)
        self.assertTrue(any("placeholder" in mensagem for mensagem in erros))


class TestPoliticaVersionada(unittest.TestCase):
    def setUp(self) -> None:
        self.reels, self.stories = carregar_bases()
        operacionalizar(self.reels, self.stories)

    def test_fila_operacional_valida_aceita_reel_60_e_story_59(self) -> None:
        self.reels["conteudos"] = [reel(duracao=60.0)]
        self.stories["pacotes"] = [story(duracao=59.0)]

        self.assertEqual(validar_filas(self.reels, self.stories), [])

    def test_story_de_60_segundos_falha_no_teto_rigido(self) -> None:
        self.stories["pacotes"] = [story(duracao=60.0)]

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("59" in mensagem and "duração" in mensagem for mensagem in erros))

    def test_limite_declarado_nao_pode_relaxar_teto_de_59(self) -> None:
        for fila in (self.reels, self.stories):
            fila["politica"]["stories"]["limite_parte_segundos"] = 120
        self.stories["limite_por_parte_segundos"] = 120
        self.stories["pacotes"] = [story(duracao=58.0)]

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("nunca ultrapassar 59" in mensagem for mensagem in erros))

    def test_schema_e_canal_errados_falham(self) -> None:
        self.reels["schema_version"] = 2
        self.stories["canal"] = "instagram-facebook-reels"

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("schema_version" in mensagem for mensagem in erros))
        self.assertTrue(any("canal deve ser" in mensagem for mensagem in erros))

    def test_rampa_versionada_errada_falha(self) -> None:
        politica_invalida = [1, 1, 3, 4, 5]
        for fila in (self.reels, self.stories):
            fila["politica"]["reels"]["quantidade_por_semana"] = copy.deepcopy(
                politica_invalida
            )

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("quantidade_por_semana" in mensagem for mensagem in erros))

    def test_slot_fora_do_prefixo_da_rampa_falha(self) -> None:
        self.reels["conteudos"] = [reel(horario="09:00")]

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("rampa/prefixo" in mensagem for mensagem in erros))

    def test_aprovacao_prefixo_bloqueado_e_unicidade_sao_exigidos(self) -> None:
        primeiro = reel()
        segundo = copy.deepcopy(primeiro)
        primeiro["aprovado"] = False
        primeiro["origem"]["arquivo"] = "932 - divergente.mp4"
        segundo["id"] = "outro-id"
        segundo["horario"] = "09:00"
        self.reels["conteudos"] = [primeiro, segundo]

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("aprovado" in mensagem for mensagem in erros))
        self.assertTrue(any("prefixo bloqueado" in mensagem for mensagem in erros))
        self.assertTrue(any("asset duplicado" in mensagem for mensagem in erros))
        self.assertTrue(any("SHA-256 de mídia duplicado" in mensagem for mensagem in erros))

    def test_estado_de_plataforma_desconhecido_falha(self) -> None:
        item = reel()
        item["instagram"]["status"] = "agendado"
        self.reels["conteudos"] = [item]

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("status inválido" in mensagem for mensagem in erros))

    def test_facebook_processando_com_video_id_e_valido(self) -> None:
        item = reel()
        item["facebook"].update(
            {
                "status": "processando",
                "video_id": "video-123",
                "upload_url": "https://rupload.facebook.com/video-upload/v23.0/video-123",
                "fase": "publicacao_aceita",
            }
        )
        self.reels["conteudos"] = [item]

        self.assertEqual(validar_filas(self.reels, self.stories), [])

    def test_facebook_em_envio_exige_upload_url_canonica(self) -> None:
        item = reel()
        item["facebook"].update(
            {"status": "enviando", "video_id": "video-123", "fase": "sessao_criada"}
        )
        self.reels["conteudos"] = [item]

        erros = validar_filas(self.reels, self.stories)
        self.assertTrue(any("upload_url obrigatória" in mensagem for mensagem in erros))

        item["facebook"]["upload_url"] = "https://graph.facebook.com/video-upload/v23.0/video-123"
        erros = validar_filas(self.reels, self.stories)
        self.assertTrue(any("URL canônica" in mensagem for mensagem in erros))

    def test_checkpoint_de_id_abandonado_permanece_fila_valida(self) -> None:
        item = reel()
        item["facebook"] = {
            "status": "erro",
            "fase": "identificador_abandonado",
            "legenda": LEGENDA,
            "erro": "Facebook informou falha conclusiva",
            "ultima_tentativa_em": "2026-09-14T05:01:00-03:00",
            "videos_abandonados": [
                {
                    "id": "video-morto",
                    "abandonado_em": "2026-09-14T05:01:00-03:00",
                    "motivo": "failed",
                    "estado_meta": "{}",
                }
            ],
        }
        self.reels["conteudos"] = [item]

        self.assertEqual(validar_filas(self.reels, self.stories), [])

    def test_alerta_instagram_sem_media_id_e_fase_valida(self) -> None:
        item = reel()
        item["instagram"].update(
            {
                "status": "processando",
                "fase": "publicado_sem_media_id",
                "container_id": "container-123",
                "reconciliacao_manual": {
                    "necessaria": True,
                    "motivo": "resposta ambígua",
                    "detectada_em": "2026-09-14T05:01:00-03:00",
                },
            }
        )
        self.reels["conteudos"] = [item]

        self.assertEqual(validar_filas(self.reels, self.stories), [])

    def test_fase_desconhecida_falha_antes_do_merge(self) -> None:
        item = reel()
        item["instagram"].update(
            {
                "status": "processando",
                "fase": "fase_inventada",
                "container_id": "container-123",
            }
        )
        self.reels["conteudos"] = [item]

        erros = validar_filas(self.reels, self.stories)
        self.assertTrue(any("fase desconhecida" in mensagem for mensagem in erros))

    def test_intencao_de_remocao_exige_publicacao_e_destino_correto(self) -> None:
        item = reel()
        item["midia"].update(
            {
                "remocao_solicitada_em": "2026-09-14T05:01:00-03:00",
                "remocao_repositorio": "outro/repo",
                "remocao_release_tag": TAG,
            }
        )
        self.reels["conteudos"] = [item]

        erros = validar_filas(self.reels, self.stories)

        self.assertTrue(any("remoção só pode" in mensagem for mensagem in erros))
        self.assertTrue(any("intenção de remoção diverge" in mensagem for mensagem in erros))


if __name__ == "__main__":
    unittest.main()
