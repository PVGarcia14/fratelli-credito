import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

APP_DIR = Path(__file__).parent
DB = APP_DIR / "fratelli_credito.db"
LOGO = APP_DIR / "assets" / "fratelli_logo.png"

st.set_page_config(page_title="Fratelli Crédito 4.1", page_icon="💳", layout="wide")

WEIGHTS = {
    "Cadastro e estabilidade": 20,
    "Capacidade financeira": 25,
    "Histórico de pagamento": 30,
    "Exposição": 15,
    "Comportamento operacional": 10,
}

SEGMENT_FACTORS = {
    "Bar": 0.06,
    "Restaurante": 0.07,
    "Distribuidor": 0.10,
    "Supermercado": 0.08,
    "Empório": 0.07,
    "Hotel": 0.07,
    "Outro": 0.05,
}

SERASA_UAT_BASE = "https://uat-api.serasaexperian.com.br/credit-services/business-information-report/v1/reports"

def serasa_uat_test(token, report_name, query_params, timeout=20):
    """Teste genérico do endpoint UAT informado pelo usuário.
    Não assume parâmetros proprietários além de reportName.
    """
    if not token.strip():
        return None, "Informe um Bearer Token da Serasa para testar."
    params = {"reportName": report_name}
    params.update({k: v for k, v in query_params.items() if str(v).strip()})
    headers = {
        "Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(SERASA_UAT_BASE, params=params, headers=headers, timeout=timeout)
        try:
            payload = r.json()
        except Exception:
            payload = r.text
        return {"status_code": r.status_code, "url": r.url, "payload": payload}, None
    except requests.RequestException as e:
        return None, f"Erro de conexão: {e}"

def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS clientes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cnpj TEXT UNIQUE,
        razao TEXT, fantasia TEXT, segmento TEXT,
        criado_em TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS analises(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER,
        data TEXT,
        score REAL,
        risco TEXT,
        cobertura REAL,
        limite REAL,
        limite_disponivel REAL,
        pedido REAL,
        prazo TEXT,
        decisao TEXT,
        dados TEXT
    )""")
    con.commit()
    return con

def clean_cnpj(x):
    return "".join(c for c in str(x) if c.isdigit())

def money(x):
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def cnpj_lookup(cnpj):
    cnpj = clean_cnpj(cnpj)
    if len(cnpj) != 14:
        return None, "CNPJ inválido."
    urls = [
        f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}",
        f"https://publica.cnpj.ws/cnpj/{cnpj}",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            if r.ok:
                return r.json(), "Consulta pública realizada."
        except Exception:
            pass
    return None, "Não foi possível consultar o CNPJ agora."

def score_cadastro(situacao, anos):
    vals = []
    if situacao:
        s = situacao.upper()
        if "ATIVA" in s or "REGULAR" in s:
            vals.append(100)
        elif "SUSPEN" in s or "INAPTA" in s:
            vals.append(35)
        else:
            vals.append(10)
    if anos is not None:
        if anos >= 10: vals.append(100)
        elif anos >= 5: vals.append(90)
        elif anos >= 2: vals.append(75)
        elif anos >= 1: vals.append(60)
        else: vals.append(40)
    return sum(vals)/len(vals) if vals else None

def score_capacidade(faturamento, fonte, pedido):
    # Faturamento autodeclarado é exibido, mas não recebe a mesma confiança
    # de documento financeiro. Ainda pode servir como sinal, nunca como "fato".
    if faturamento is None or faturamento <= 0:
        return None
    if pedido <= 0:
        return 70
    ratio = pedido / faturamento
    base = 100 if ratio <= .03 else 90 if ratio <= .05 else 75 if ratio <= .08 else 55 if ratio <= .12 else 30
    if fonte == "Declaração do cliente":
        base = min(base, 70)
    return base

def score_pagamento(compras, atrasos, maior_atraso, pontualidade):
    vals = []
    if compras is not None and compras > 0:
        vals.append(80 if compras >= 12 else 65)
    if atrasos is not None:
        vals.append(100 if atrasos == 0 else 70 if atrasos <= 2 else 40 if atrasos <= 5 else 15)
    if maior_atraso is not None:
        vals.append(100 if maior_atraso == 0 else 80 if maior_atraso <= 7 else 60 if maior_atraso <= 30 else 30 if maior_atraso <= 90 else 0)
    if pontualidade is not None:
        vals.append(max(0, min(100, pontualidade)))
    return sum(vals)/len(vals) if vals else None

def score_exposicao(faturamento, aberto, limite_atual):
    if faturamento is None or faturamento <= 0:
        return None
    exposicao = (aberto + max(limite_atual, 0)) / faturamento
    return 100 if exposicao <= .03 else 85 if exposicao <= .06 else 70 if exposicao <= .10 else 45 if exposicao <= .15 else 20

def score_operacional(restricoes, fonte_restricoes):
    # Aqui, somente informação efetivamente fornecida é considerada.
    if restricoes is None:
        return None
    return 100 if restricoes == 0 else 55 if restricoes <= 2 else 20

def calc_score(items):
    total_weight = sum(WEIGHTS.values())
    used = sum(WEIGHTS[k] for k,v in items.items() if v is not None)
    if used == 0:
        return None, 0
    score = sum(WEIGHTS[k]*v for k,v in items.items() if v is not None) / used
    return round(score, 1), round(used/total_weight*100, 1)

def risk(score, situacao, maior_atraso):
    if score is None:
        return "NÃO CLASSIFICADO"
    if situacao and ("ATIVA" not in situacao.upper() and "REGULAR" not in situacao.upper()):
        return "ELEVADO"
    if maior_atraso is not None and maior_atraso > 90:
        return "ELEVADO"
    if score >= 80: return "CONTROLADO"
    if score >= 60: return "MODERADO"
    return "ELEVADO"

def limit_credit(score, faturamento, segmento, compras_12m, atrasos, pontualidade):
    if score is None or score < 60:
        return 0
    # Sem faturamento comprovável, não inventar capacidade.
    if faturamento is None or faturamento <= 0:
        if compras_12m and compras_12m > 0 and atrasos == 0 and (pontualidade is None or pontualidade >= 90):
            return min(5000, compras_12m * 0.50)
        return 0
    factor = SEGMENT_FACTORS.get(segmento, .05)
    bruto = faturamento * factor
    if score < 80:
        bruto = min(bruto, 12000)
    else:
        bruto = min(bruto, 30000)
    return max(0, round(bruto, -2))

def conditions(risco, limite_disp, pedido):
    if risco == "ELEVADO":
        return "À vista", 0, "CRÉDITO NÃO RECOMENDADO"
    if risco == "NÃO CLASSIFICADO":
        return "Pendente de informações", 0, "PENDENTE"
    if pedido <= limite_disp and pedido > 0:
        if risco == "CONTROLADO":
            return ("30 dias" if pedido <= 5000 else "30/60 dias"), 0, "APROVADO"
        return "Entrada + 30 dias", round(pedido*.30, 2), "APROVADO COM CONDIÇÃO"
    if risco == "CONTROLADO":
        return "Entrada + 30 dias", round(pedido*.30, 2), "APROVAÇÃO CONDICIONADA"
    return "À vista", 0, "NÃO APROVADO PARA ESTE PEDIDO"


def source_reliability(source):
    return {
        "Documento financeiro": 1.00,
        "Fonte financeira autorizada": 1.00,
        "Serasa/SPC — consulta autorizada": 1.00,
        "Open Finance — dados consentidos": 1.00,
        "Documento cadastral oficial": 0.95,
        "Histórico interno Fratelli": 0.95,
        "Declaração do cliente": 0.55,
        "Não informado": 0.00,
    }.get(source, 0.50)

def external_score_normalized(score):
    if score is None:
        return None
    return max(0, min(100, float(score)))

def decision_gate(cnpj_ok, score, coverage, restrictions_confirmed, active_restrictions,
                  largest_delay, fraud_alert=False):
    """Gates are controls, not extra score points."""
    if not cnpj_ok:
        return "BLOQUEADO", "CNPJ não validado."
    if fraud_alert:
        return "REVISÃO MANUAL", "Sinal de fraude/alerta cadastral requer revisão humana."
    if active_restrictions is not None and active_restrictions > 0:
        if score is not None and score >= 80:
            return "REVISÃO MANUAL", "Há restrição ativa confirmada; não liberar automaticamente."
        return "BLOQUEADO", "Há restrição ativa confirmada."
    if largest_delay is not None and largest_delay > 90:
        return "REVISÃO MANUAL", "Maior atraso superior a 90 dias."
    if coverage < 50:
        return "PENDENTE", "Cobertura de dados inferior a 50%."
    if score is None:
        return "PENDENTE", "Score interno não pôde ser calculado."
    return "OK", "Critérios mínimos atendidos."

def data_quality_label(coverage, verified_sources):
    if coverage >= 80 and verified_sources >= 3:
        return "ALTA"
    if coverage >= 50 and verified_sources >= 2:
        return "MÉDIA"
    return "BAIXA"


def pdf_report(data, path):
    if not REPORTLAB_OK:
        return False
    c = canvas.Canvas(str(path), pagesize=A4)
    w,h = A4
    y = h-50
    c.setFont("Helvetica-Bold", 18)
    c.drawString(45,y,"Fratelli Crédito 4.1")
    y -= 30
    c.setFont("Helvetica",10)
    for label, value in data:
        c.drawString(45,y,f"{label}: {value}")
        y -= 18
        if y < 50:
            c.showPage(); y=h-50
    c.save()
    return True

con = db()

st.title("Fratelli Crédito 4.1")
st.caption("Análise empresarial para decisão de crédito — dados ausentes são excluídos do cálculo.")

menu = st.sidebar.radio("Menu", ["Nova análise", "Teste Serasa UAT", "Histórico", "Clientes", "Metodologia"])

if menu == "Nova análise":
    st.subheader("1. Cadastro e consulta")
    c1,c2,c3 = st.columns(3)
    with c1:
        cnpj = st.text_input("CNPJ")
        if st.button("Consultar CNPJ"):
            data,msg = cnpj_lookup(cnpj)
            st.session_state["cnpj_data"] = data
            st.info(msg)
    data = st.session_state.get("cnpj_data") or {}
    with c2:
        razao = st.text_input("Razão social", value=data.get("razao_social",""))
        fantasia = st.text_input("Nome fantasia", value=data.get("nome_fantasia",""))
    with c3:
        segmento = st.selectbox("Segmento", list(SEGMENT_FACTORS.keys()))

    st.subheader("2. Capacidade e pedido")
    a,b,c,d = st.columns(4)
    with a: faturamento = st.number_input("Faturamento mensal (R$)", min_value=0.0, step=1000.0)
    with b: fonte = st.selectbox("Fonte do faturamento", ["Não informado","Documento financeiro","Fonte financeira autorizada","Declaração do cliente"])
    with c: pedido = st.number_input("Valor do pedido (R$)", min_value=0.0, step=100.0)
    with d: prazo_pedido = st.selectbox("Prazo solicitado", ["À vista","30 dias","30/60 dias","60 dias","90 dias"])
    e1,e2 = st.columns(2)
    with e1:
        bureau_source = st.selectbox("Fonte de score externo", ["Não informado","Serasa/SPC — consulta autorizada"])
    with e2:
        bureau_score = st.number_input("Score externo (0–1000, se disponível)", min_value=0.0, max_value=1000.0, value=0.0, step=1.0)


    st.subheader("3. Histórico e exposição")
    a,b,c,d = st.columns(4)
    with a: compras_12m = st.number_input("Compras Fratelli em 12 meses (R$)", min_value=0.0, step=1000.0)
    with b: aberto = st.number_input("Valor atualmente em aberto (R$)", min_value=0.0, step=500.0)
    with c: limite_atual = st.number_input("Limite atual (R$)", min_value=0.0, step=500.0)
    with d: pontualidade = st.number_input("% de pagamentos no prazo", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
    e,f,g = st.columns(3)
    with e: atrasos = st.number_input("Quantidade de atrasos", min_value=0, step=1)
    with f: maior_atraso = st.number_input("Maior atraso (dias)", min_value=0, step=1)
    with g: restricoes = st.number_input("Restrições/protestos confirmados", min_value=0, step=1)

    st.caption("Se um dado não for conhecido, deixe-o sem informação/zero apenas quando o zero for um fato confirmado. Para análises rigorosas, diferencie 'zero confirmado' de 'não informado'.")

    # Explicit checkbox avoids confusing unknown with zero.
    x,y,z = st.columns(3)
    with x: tem_historico = st.checkbox("Tenho histórico de pagamentos")
    with y: tem_pontualidade = st.checkbox("Tenho % de pontualidade confirmado")
    with z: tem_restricoes = st.checkbox("Tenho consulta de restrições confirmada")

    itens = {
        "Cadastro e estabilidade": score_cadastro(
            data.get("descricao_situacao_cadastral"),
            ((datetime.now()-datetime.strptime(data["data_inicio_atividade"], "%Y-%m-%d")).days/365.25 if data.get("data_inicio_atividade") else None)
        ),
        "Capacidade financeira": score_capacidade(
            faturamento if faturamento > 0 else None,
            fonte, pedido
        ),
        "Histórico de pagamento": score_pagamento(
            compras_12m if tem_historico and compras_12m > 0 else None,
            atrasos if tem_historico else None,
            maior_atraso if tem_historico else None,
            pontualidade if tem_pontualidade else None
        ),
        "Exposição": score_exposicao(
            faturamento if faturamento > 0 else None,
            aberto, limite_atual
        ),
        "Comportamento operacional": score_operacional(
            restricoes if tem_restricoes else None, fonte
        ),
    }
    score,cobertura = calc_score(itens)

    # External bureau information is displayed separately to avoid double-counting.
    bureau_norm = None if bureau_source == "Não informado" or bureau_score <= 0 else round(bureau_score / 10, 1)
    verified_sources = 0
    if data: verified_sources += 1
    if faturamento > 0 and fonte in ("Documento financeiro","Fonte financeira autorizada"): verified_sources += 1
    if bureau_norm is not None: verified_sources += 1
    if tem_historico: verified_sources += 1
    if tem_restricoes: verified_sources += 1
    quality = data_quality_label(cobertura, verified_sources)

    situacao = data.get("descricao_situacao_cadastral","")
    risco = risk(score,situacao,maior_atraso if tem_historico else None)
    gate, gate_reason = decision_gate(
        cnpj_ok=bool(data),
        score=score,
        coverage=cobertura,
        restrictions_confirmed=tem_restricoes,
        active_restrictions=(restricoes if tem_restricoes else None),
        largest_delay=(maior_atraso if tem_historico else None),
    )

    limite = limit_credit(score,faturamento if faturamento>0 else None,segmento,compras_12m,atrasos,pontualidade if tem_pontualidade else None)
    disponivel = max(0, limite - aberto)
    prazo, entrada, decisao = conditions(risco,disponivel,pedido)
    if gate == "BLOQUEADO":
        decisao = "NÃO APROVADO"
        prazo, entrada = "À vista", 0
    elif gate in ("REVISÃO MANUAL", "PENDENTE"):
        decisao = gate
        prazo, entrada = "Revisão manual / pendente", 0


    st.divider()
    st.subheader("4. Resultado")
    r1,r2,r3,r4 = st.columns(4)
    r1.metric("Score", "N/D" if score is None else score)
    r2.metric("Cobertura", f"{cobertura}%")
    r3.metric("Risco", risco)
    r4.metric("Limite recomendado", money(limite))
    st.write(f"**Qualidade dos dados:** {quality}  |  **Score externo:** {'N/D' if bureau_norm is None else f'{bureau_norm:.1f}/100 (apenas informativo)'}")
    st.write(f"**Controle de decisão:** {gate} — {gate_reason}")


    st.write(f"**Limite disponível para este pedido:** {money(disponivel)}")
    st.write(f"**Condição sugerida:** {prazo}  |  **Entrada:** {money(entrada)}")
    st.write(f"**Decisão:** {decisao}")

    with st.expander("Ver composição do score"):
        for k,v in itens.items():
            st.write(f"**{k}** — {'N/D' if v is None else f'{v:.1f}/100'} — peso {WEIGHTS[k]}%")

    justificativas = []
    if score is None: justificativas.append("Não há dados suficientes para classificar o risco.")
    if faturamento <= 0: justificativas.append("Sem faturamento informado/comprovado, não foi calculado limite por capacidade.")
    if fonte == "Declaração do cliente": justificativas.append("Faturamento declarado recebeu confiança menor que documentação financeira.")
    if tem_restricoes and restricoes > 0: justificativas.append(f"Foram informadas {restricoes} restrição(ões)/protesto(s) confirmado(s).")
    if tem_historico and atrasos > 0: justificativas.append(f"Há {atrasos} atraso(s); maior atraso informado: {maior_atraso} dia(s).")
    if bureau_norm is not None:
        justificativas.append("Score externo foi mantido separado do score interno para evitar dupla contagem.")
    if quality == "BAIXA":
        justificativas.append("Qualidade de dados baixa: priorizar revisão humana antes de ampliar limite.")
    if not justificativas:
        justificativas.append("Decisão baseada exclusivamente em dados efetivamente informados ou consultados.")

    st.info(" ".join(justificativas))

    if st.button("Salvar análise"):
        cnpj_clean = clean_cnpj(cnpj)
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO clientes(cnpj,razao,fantasia,segmento,criado_em) VALUES(?,?,?,?,?)",
                    (cnpj_clean,razao,fantasia,segmento,datetime.now().isoformat()))
        cur.execute("SELECT id FROM clientes WHERE cnpj=?", (cnpj_clean,))
        cid = cur.fetchone()[0]
        cur.execute("""INSERT INTO analises(cliente_id,data,score,risco,cobertura,limite,limite_disponivel,pedido,prazo,decisao,dados)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid,datetime.now().isoformat(),score,risco,cobertura,limite,disponivel,pedido,prazo,decisao,str(itens)))
        con.commit()
        st.success("Análise salva com sucesso.")

        if REPORTLAB_OK:
            pdf = APP_DIR / f"relatorio_{cnpj_clean or 'cliente'}.pdf"
            pdf_report([
                ("CNPJ",cnpj_clean),("Razão social",razao),("Segmento",segmento),
                ("Score","N/D" if score is None else score),("Cobertura",f"{cobertura}%"),
                ("Risco",risco),("Limite recomendado",money(limite)),
                ("Limite disponível",money(disponivel)),("Pedido",money(pedido)),
                ("Prazo sugerido",prazo),("Entrada",money(entrada)),("Decisão",decisao)
            ],pdf)
            with open(pdf,"rb") as f:
                st.download_button("Baixar relatório PDF",f,file_name=pdf.name)

elif menu == "Teste Serasa UAT":
    st.subheader("Teste de integração — Serasa Experian UAT")
    st.warning("Este módulo é apenas para homologação. Não coloque client_secret ou outras credenciais permanentes no código.")

    st.write("Endpoint base configurado:")
    st.code(SERASA_UAT_BASE)

    token = st.text_input("Bearer Token", type="password",
                          help="Cole um token de acesso temporário fornecido pela Serasa. O token não é salvo no banco.")
    report_name = st.text_input("reportName", value="",
                                help="Use exatamente o reportName definido na documentação/contrato da Serasa.")

    st.markdown("**Parâmetros adicionais da consulta**")
    q1, q2, q3 = st.columns(3)
    with q1:
        p1_name = st.text_input("Parâmetro 1", value="")
        p1_value = st.text_input("Valor 1", value="")
    with q2:
        p2_name = st.text_input("Parâmetro 2", value="")
        p2_value = st.text_input("Valor 2", value="")
    with q3:
        p3_name = st.text_input("Parâmetro 3", value="")
        p3_value = st.text_input("Valor 3", value="")

    st.caption("Os nomes dos parâmetros adicionais devem vir da documentação da Serasa. O sistema não inventa o nome do parâmetro do CNPJ.")

    params = {}
    for n, v in [(p1_name,p1_value),(p2_name,p2_value),(p3_name,p3_value)]:
        if n.strip() and v.strip():
            params[n.strip()] = v.strip()

    if st.button("Testar Serasa UAT"):
        if not report_name.strip():
            st.error("Informe o reportName.")
        else:
            result, err = serasa_uat_test(token, report_name, params)
            if err:
                st.error(err)
            else:
                st.write(f"HTTP **{result['status_code']}**")
                if result["status_code"] >= 200 and result["status_code"] < 300:
                    st.success("Endpoint respondeu com sucesso.")
                elif result["status_code"] in (401,403):
                    st.error("A API respondeu, mas a autenticação/credencial não foi aceita.")
                else:
                    st.warning("A API respondeu. Veja o corpo abaixo para identificar parâmetros ou permissões necessários.")
                st.code(str(result["payload"])[:20000], language="json")
                st.caption(f"URL efetivamente chamada: {result['url']}")

elif menu == "Histórico":
    st.subheader("Histórico de análises")
    df = pd.read_sql_query("""SELECT a.id,a.data,c.cnpj,c.razao,c.segmento,a.score,a.risco,a.cobertura,
                              a.limite,a.limite_disponivel,a.pedido,a.prazo,a.decisao
                              FROM analises a JOIN clientes c ON c.id=a.cliente_id ORDER BY a.id DESC""", con)
    st.dataframe(df, use_container_width=True)
elif menu == "Clientes":
    st.subheader("Clientes cadastrados")
    df = pd.read_sql_query("SELECT * FROM clientes ORDER BY id DESC", con)
    st.dataframe(df, use_container_width=True)
else:
    st.subheader("Metodologia 4.2")
    st.markdown("""
**Regra central:** informação ausente não é boa nem ruim. Ela é excluída do cálculo.

**Pesos**
- Cadastro e estabilidade: 20%
- Capacidade financeira: 25%
- Histórico de pagamento: 30%
- Exposição: 15%
- Comportamento operacional: 10%

**Cobertura:** mostra quanto da política total pôde ser efetivamente analisado.

**Faturamento:** a fonte é identificada. Declaração do cliente não é tratada como equivalente a documento financeiro.

**Limite:** não é inventado quando não existe base de capacidade. Histórico interno pode servir como base limitada quando houver comportamento comprovado.

**Restrições:** somente entram no cálculo quando a consulta foi efetivamente confirmada e registrada.

**Decisão:** o sistema separa risco, limite disponível e condição comercial sugerida.

**Fontes externas:** scores de bureaus (Serasa/SPC) são armazenados como evidência externa e não são somados diretamente ao score interno, evitando dupla contagem.

**Gates de segurança:** CNPJ não validado, restrição ativa, atraso >90 dias, sinal de fraude ou baixa cobertura podem impedir aprovação automática ou exigir revisão humana.

**Qualidade dos dados:** cada análise recebe uma classificação de qualidade com base na cobertura e na quantidade de fontes verificadas.

**Princípio de explicabilidade:** toda decisão deve mostrar os dados usados, as fontes, o que ficou de fora e o motivo da decisão.

""")
