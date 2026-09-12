# Publicador Meta — Pílula Diária

Repositório-base do publicador de Reels e Stories do Pílula Diária. A mídia
fica temporariamente na Release indicada por `RELEASE_TAG` (por padrão,
`fila-instagram-facebook`); ela não entra no histórico Git e só é removida
depois que Instagram e Facebook confirmarem a publicação e o estado da fila já
estiver persistido no Git.

## Agenda

- Reels: 05:00, 09:00, 13:00, 17:00 e 21:00 em `America/Sao_Paulo`.
- Stories: um pacote às 09:00; o Reel das 09:00 roda em job independente e em
  paralelo, para um pacote longo não atrasar o outro formato.
- Cada horário tem uma retentativa 15 minutos depois. Ela consulta e reutiliza o
  mesmo `container_id`/`video_id`, nunca troca silenciosamente para uma nova
  operação quando já existe uma em andamento.
- A fila determina quais horários existem durante o aquecimento; por isso os
  workflows podem disparar cinco vezes sem publicar itens ainda não habilitados.

Os scripts publicam **somente o item da data e do horário explicitamente
informados pelo workflow**. Itens atrasados não são despejados automaticamente
em outro horário.

O checkout usa explicitamente a ponta atual da branch padrão depois que o job
adquire o lock de concorrência. Essa proteção corrige uma falha observada no
projeto de referência, em que dois dispatches enfileirados partiram do mesmo SHA
antigo e tentaram novamente um Story já publicado.

Cada transição crítica da operação também é commitada e enviada à branch antes
da chamada irreversível seguinte. Um `non-fast-forward` aciona retry limitado e
merge semântico por item/slot, que valida a fila e nunca usa `force-push`. A
limpeza cria primeiro uma intenção durável, confere nome, tamanho e SHA-256 do
asset e só então o remove. Isso permite recuperar uma queda entre o `DELETE` e o
marcador final sem presumir que um asset ausente foi publicado.

Se o Instagram disser que um container foi publicado sem devolver o ID real do
objeto de mídia, o estado recebe um alerta `reconciliacao_manual` e o asset não é
apagado. O container ID nunca é gravado como se fosse o Media ID. Identificadores
que a Meta confirme como `ERROR`, `FAILED` ou `EXPIRED` ficam arquivados no
histórico antes de outra sessão ser criada.

## Configuração no GitHub

Segredos obrigatórios:

- `IG_ACCESS_TOKEN`
- `IG_BUSINESS_ID`
- `FB_PAGE_ACCESS_TOKEN`
- `FB_PAGE_ID`

Variáveis:

- `PUBLICACAO_ATIVA`: mantenha ausente ou `false` até o teste controlado; use
  `true` somente após aprovação.
- `META_GRAPH_VERSION`: versão validada da Graph API. Se ausente, o template usa
  `v23.0`, igual ao projeto de referência no momento da réplica.
- `RELEASE_TAG`: tag da Release transitória de mídia. Se ausente, o template usa
  `fila-instagram-facebook`.

O repositório/Release precisa oferecer uma URL realmente pública para que o
Instagram consiga ingerir os vídeos. Portanto, a cópia transitória fica
publicamente acessível enquanto aguarda o slot. Nunca grave tokens, cookies ou
credenciais nos arquivos ou nas filas.

## Execução manual segura

O workflow de Reels aceita `data_publicacao` e `horario_publicacao`; o de 09h
aceita a data e fixa o horário. Ambos exigem selecionar explicitamente
`PUBLICAR` na execução manual, além da trava `PUBLICACAO_ATIVA=true`. Se não
houver item aprovado e pendente exatamente no slot, o comando termina sem
publicar.
