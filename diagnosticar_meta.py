"""Confere identidades e permissões básicas sem enviar ou publicar mídia."""

from __future__ import annotations

from meta_comum import graph_get, obrigatoria


def main() -> None:
    ig_id = obrigatoria("IG_BUSINESS_ID")
    ig_token = obrigatoria("IG_ACCESS_TOKEN")
    fb_id = obrigatoria("FB_PAGE_ID")
    fb_token = obrigatoria("FB_PAGE_ACCESS_TOKEN")
    instagram = graph_get(
        ig_id,
        {"fields": "id,username", "access_token": ig_token},
    )
    facebook = graph_get(
        fb_id,
        {"fields": "id,name", "access_token": fb_token},
    )
    print(f"Instagram acessível: @{instagram.get('username')} ({instagram.get('id')})")
    print(f"Página Facebook acessível: {facebook.get('name')} ({facebook.get('id')})")
    try:
        limite = graph_get(
            f"{ig_id}/content_publishing_limit",
            {"fields": "quota_usage,config", "access_token": ig_token},
        )
        print(f"Limite de publicação consultado: {limite}")
    except Exception as erro:
        print(f"AVISO: não foi possível consultar content_publishing_limit: {erro}")


if __name__ == "__main__":
    main()
