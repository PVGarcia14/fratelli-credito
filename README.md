# Fratelli Crédito 2.0

Sistema profissional inicial para decisão de crédito B2B.

## Executar
pip install -r requirements.txt
streamlit run app.py

## Motor
Score 0–100 com:
- idade da empresa
- histórico de pagamentos
- capacidade de pagamento
- comportamento de atraso
- relacionamento com a Fratelli
- cadastro/ocorrências
- situação cadastral
- exposição atual
- limite já utilizado
- segmento comercial

## Decisão
O sistema calcula:
- risco
- limite disponível
- prazo
- entrada necessária
- decisão: aprovar, aprovar com condição, reduzir limite/entrada ou venda à vista.

## Integração
A tela Integração CNPJ define a interface para conectar uma API autorizada de dados cadastrais. A API concreta não é incluída porque exige credencial/contrato do provedor.
