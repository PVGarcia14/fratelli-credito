# Sistema de Análise de Crédito 3.0

Sistema web em Streamlit para análise de crédito empresarial.

## Novidades
- Consulta automática de CNPJ.
- Preenchimento de dados cadastrais disponíveis.
- Score, risco, limite, prazo e entrada.
- Histórico de análises.
- Dashboard.
- Política de crédito configurável.
- Exportação CSV.
- Relatório PDF.
- Suporte a CNPJ alfanumérico conforme a mudança recente do cadastro.
- Logo personalizada: coloque um arquivo `logo.png` na mesma pasta do `app.py`.

## Importante
A consulta cadastral não fornece automaticamente todos os dados necessários para crédito, como faturamento, histórico de pagamentos, exposição e protestos. Esses dados devem vir de fonte autorizada ou de sistemas internos.

## Rodar localmente
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Publicar
Suba `app.py`, `requirements.txt` e `README.md` para um repositório GitHub e faça o deploy em uma plataforma compatível com Streamlit.
