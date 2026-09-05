# Consulta de Autopeças — Balcão

MVP independente para consulta de aplicações de autopeças. A primeira base usa o catálogo AuthoMix de abril de 2022 e a estrutura aceita novos catálogos.

## Executar localmente

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
python scripts/import_catalog.py catalogo.pdf data/catalog.json
set BALCAO_USER=vendedor
set BALCAO_PASSWORD=uma-senha-forte
python app.py
```

No Linux/macOS, substitua `set` por `export`. Acesse `http://localhost:5000`.

## Publicar no Render

O arquivo `render.yaml` já descreve o serviço. Crie um Blueprint no Render a partir do repositório e informe `BALCAO_PASSWORD` quando solicitado. O usuário padrão configurado é `vendedor`.

## Importar outro catálogo

Execute o importador indicando fabricante e edição. Salve cada catálogo como um arquivo `.json` diferente dentro de `data/`; a aplicação carrega todos automaticamente, sem mudança na tela.

```bash
python scripts/import_catalog.py novo-catalogo.pdf data/novo.json --manufacturer "Fabricante" --edition "2026"
```

## Observação de segurança

A resposta confirma somente o que foi localizado no catálogo. Antes da venda, confira eventuais restrições de motor, versão, posição, lado e sistema do veículo.
