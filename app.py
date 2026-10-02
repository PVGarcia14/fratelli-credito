import os
import io
import re
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

APP_TITLE = "Sistema de Análise de Crédito Empresarial"
DB_PATH = "credito_empresarial.db"
TIMEOUT = 12

st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")


# -----------------------------
# Helpers
# -----------------------------
def clean_cnpj(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def valid_cnpj(cnpj: str) -> bool:
    cnpj = clean_cnpj(cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    nums = [int(x) for x in cnpj]
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = sum(a * b for a, b in zip(nums[:12], w1)) % 11
    d1 = 0 if d1 < 2 else 11 - d1
    d2 = sum(a * b for a, b in zip(nums[:12] + [d1], w2)) % 11
    d2 = 0 if d2 < 2 else 11 - d2
    return d1 == nums[12] and d2 == nums[13]


def money(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def safe_float(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def parse_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date):
        return v
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def years_active(start: Optional[date]) -> float:
    if not start:
        return 0.0
    return max(0.0, (date.today() - start).days / 365.25)


# -----------------------------
# Database
# -----------------------------
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            cnpj TEXT PRIMARY KEY,
            razao_social TEXT,
            nome_fantasia TEXT,
            situacao TEXT,
            abertura TEXT,
            capital_social REAL,
            porte TEXT,
            natureza_juridica TEXT,
            cnae TEXT,
            endereco TEXT,
            cidade TEXT,
            uf TEXT,
            socios TEXT,
            fonte_cnpj TEXT,
            consultado_em TEXT,
            faturamento_mensal REAL DEFAULT 0,
            faturamento_fonte TEXT DEFAULT 'Não informado',
            compras_12m REAL DEFAULT 0,
            exposicao_atual REAL DEFAULT 0,
            pedido REAL DEFAULT 0,
            pagamentos_total INTEGER DEFAULT 0,
            pagamentos_em_dia INTEGER DEFAULT 0,
            titulos_atrasados INTEGER DEFAULT 0,
            valor_atrasado REAL DEFAULT 0,
            atraso_medio REAL DEFAULT 0,
            maior_atraso REAL DEFAULT 0,
            devolucoes INTEGER DEFAULT 0,
            protestos INTEGER DEFAULT 0,
            garantias REAL DEFAULT 0,
            observacoes TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cnpj TEXT,
            data TEXT,
            score REAL,
            risco TEXT,
            confianca TEXT,
            limite REAL,
            exposicao REAL,
            limite_disponivel REAL,
            pedido REAL,
            entrada REAL,
            condicao TEXT,
            prazo_medio REAL,
            decisao TEXT,
            justificativa TEXT
        )
    """)
    con.commit()
    con.close()


init_db()


# -----------------------------
# CNPJ providers
# -----------------------------
def normalize_company(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    # BrasilAPI format
    est = data.get("estabelecimento") or {}
    qsa = data.get("qsa") or data.get("socios") or []
    if "razao_social" not in data and data.get("razao_social"):
        pass

    razao = data.get("razao_social") or data.get("razao") or ""
    fantasia = est.get("nome_fantasia") or data.get("nome_fantasia") or data.get("fantasia") or ""
    situacao = est.get("situacao_cadastral") or data.get("situacao") or ""
    abertura = est.get("data_inicio_atividade") or data.get("data_abertura") or data.get("abertura") or ""
    capital = data.get("capital_social") or 0

    if isinstance(capital, str):
        capital = float(re.sub(r"[^\d,.-]", "", capital).replace(".", "").replace(",", ".") or 0)

    porte = data.get("porte", "")
    if isinstance(porte, dict):
        porte = porte.get("descricao", "")

    nat = data.get("natureza_juridica", "")
    if isinstance(nat, dict):
        nat = nat.get("descricao", "")

    principal = est.get("atividade_principal") or {}
    if not principal:
        principal = data.get("atividade_principal") or {}
    cnae = principal.get("id") or principal.get("codigo") or ""
    cnae_desc = principal.get("descricao") or ""
    cnae_text = f"{cnae} - {cnae_desc}".strip(" -")

    logradouro = " ".join(filter(None, [
        est.get("tipo_logradouro"), est.get("logradouro"),
        est.get("numero"), est.get("complemento")
    ]))
    cidade = (est.get("cidade") or {})
    if isinstance(cidade, dict):
        cidade_nome = cidade.get("nome", "")
    else:
        cidade_nome = str(cidade or "")
    estado = est.get("estado") or {}
    uf = estado.get("sigla", "") if isinstance(estado, dict) else str(estado or "")
    if not logradouro:
        logradouro = data.get("endereco", "") or ""

    socios = []
    for s in qsa:
        nome = s.get("nome_socio") or s.get("nome") or ""
        qual = s.get("qualificacao_socio") or ""
        if isinstance(qual, dict):
            qual = qual.get("descricao", "")
        if nome:
            socios.append(f"{nome} ({qual})".strip())

    return {
        "cnpj": clean_cnpj(data.get("cnpj") or est.get("cnpj") or ""),
        "razao_social": razao,
        "nome_fantasia": fantasia,
        "situacao": situacao,
        "abertura": abertura,
        "capital_social": safe_float(capital),
        "porte": str(porte or ""),
        "natureza_juridica": str(nat or ""),
        "cnae": cnae_text,
        "endereco": logradouro,
        "cidade": cidade_nome,
        "uf": uf,
        "socios": "; ".join(socios),
        "fonte_cnpj": source,
        "consultado_em": datetime.now().isoformat(timespec="seconds"),
    }


def fetch_json(url: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "CreditoEmpresarial/4.0"})
        if r.status_code == 200:
            return r.json(), ""
        return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)


def consultar_cnpj(cnpj: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    cnpj = clean_cnpj(cnpj)
    errors = []

    # Current BrasilAPI endpoint documented as /cnpj/v1/{cnpj}
    data, err = fetch_json(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}")
    if data:
        return normalize_company(data, "BrasilAPI / Minha Receita"), errors
    errors.append(f"BrasilAPI: {err}")

    # CNPJ.ws public endpoint; limited to 3 requests/minute per documentation.
    data, err = fetch_json(f"https://publica.cnpj.ws/cnpj/{cnpj}")
    if data:
        return normalize_company(data, "CNPJ.ws Pública"), errors
    errors.append(f"CNPJ.ws: {err}")

    return None, errors


# -----------------------------
# Credit engine
# -----------------------------
WEIGHTS = {
    "cadastro": 10,
    "tempo": 10,
    "capacidade": 25,
    "comportamento": 25,
    "exposicao": 10,
    "relacionamento": 10,
    "externo": 10,
}

def pct_score(value: float, bands: List[Tuple[float, float]]) -> float:
    for threshold, score in bands:
        if value >= threshold:
            return score
    return 0.0


def credit_score(d: Dict[str, Any]) -> Dict[str, Any]:
    reasons = []
    points = {}

    situacao = str(d.get("situacao", "")).upper()
    cadastro = 100 if situacao == "ATIVA" else 0
    if situacao == "ATIVA":
        reasons.append("CNPJ com situação cadastral ativa.")
    else:
        reasons.append(f"Situação cadastral: {situacao or 'não informada'}.")
    points["cadastro"] = cadastro

    age = years_active(parse_date(d.get("abertura")))
    tempo = min(age / 10.0, 1.0) * 100
    points["tempo"] = tempo
    reasons.append(f"Tempo de atividade considerado: {age:.1f} anos.")

    faturamento = safe_float(d.get("faturamento_mensal"))
    pedido = safe_float(d.get("pedido"))
    if faturamento > 0 and pedido > 0:
        ratio = faturamento / pedido
        capacidade = pct_score(ratio, [(10,100),(5,85),(3,70),(1.5,50),(1,30)])
        reasons.append(f"Pedido corresponde a {pedido/faturamento:.1%} do faturamento mensal informado.")
    elif faturamento > 0:
        capacidade = 70
        reasons.append("Faturamento mensal informado, mas sem pedido para comparação.")
    else:
        capacidade = 35
        reasons.append("Faturamento mensal não disponível em fonte cadastral; capacidade financeira não pôde ser comprovada.")
    points["capacidade"] = capacidade

    total = int(d.get("pagamentos_total") or 0)
    on_time = int(d.get("pagamentos_em_dia") or 0)
    avg_delay = safe_float(d.get("atraso_medio"))
    max_delay = safe_float(d.get("maior_atraso"))
    late_value = safe_float(d.get("valor_atrasado"))
    devolucoes = int(d.get("devolucoes") or 0)

    if total > 0:
        ontime_pct = max(0, min(100, on_time / total * 100))
        punctual = pct_score(ontime_pct, [(98,100),(95,90),(90,75),(80,55),(70,35)])
        delay_component = 100 if avg_delay <= 2 else 85 if avg_delay <= 7 else 65 if avg_delay <= 15 else 40 if avg_delay <= 30 else 15
        comportamento = 0.7 * punctual + 0.3 * delay_component
        reasons.append(f"Pontualidade histórica: {ontime_pct:.1f}% em dia; atraso médio {avg_delay:.1f} dias.")
    else:
        comportamento = 40
        reasons.append("Sem histórico interno de pagamentos: análise comportamental limitada.")
    comportamento -= min(25, devolucoes * 5)
    points["comportamento"] = max(0, comportamento)

    exposicao = safe_float(d.get("exposicao_atual"))
    limite_base = max(0, faturamento * 0.10) if faturamento > 0 else 0
    if limite_base > 0:
        utilizacao = exposicao / limite_base
        exposicao_score = 100 if utilizacao <= .25 else 85 if utilizacao <= .50 else 65 if utilizacao <= .75 else 40 if utilizacao <= 1 else 10
        reasons.append(f"Exposição atual: {money(exposicao)}.")
    else:
        exposicao_score = 50
        reasons.append("Exposição informada, mas sem faturamento verificável para calcular utilização.")
    points["exposicao"] = exposicao_score

    compras = safe_float(d.get("compras_12m"))
    relacionamento = min(100, compras / 15000 * 100) if compras > 0 else 25
    points["relacionamento"] = relacionamento
    if compras > 0:
        reasons.append(f"Compras registradas nos últimos 12 meses: {money(compras)}.")
    else:
        reasons.append("Sem compras anteriores registradas.")

    # External data is not assumed. 50 = neutral/unknown, not "good".
    externo = 50
    protestos = int(d.get("protestos") or 0)
    if protestos:
        externo = max(0, 50 - protestos * 10)
        reasons.append(f"{protestos} ocorrência(s)/protesto(s) informado(s).")
    else:
        reasons.append("Não foram fornecidos dados externos de restrição; isso não equivale a ausência de restrições.")
    points["externo"] = externo

    score = sum(points[k] * WEIGHTS[k] / 100 for k in WEIGHTS)
    score = max(0, min(100, score))

    # Hard-stop rules
    hard_stop = []
    if situacao and situacao != "ATIVA":
        hard_stop.append("situação cadastral não ativa")
    if max_delay > 60:
        hard_stop.append("maior atraso informado superior a 60 dias")
    if late_value > 0 and faturamento > 0 and late_value > faturamento * 0.20:
        hard_stop.append("valor em atraso superior a 20% do faturamento mensal informado")

    if hard_stop:
        risco = "ALTO"
    elif score >= 80:
        risco = "BAIXO"
    elif score >= 60:
        risco = "MÉDIO"
    else:
        risco = "ALTO"

    data_fields = [d.get("faturamento_mensal"), d.get("pagamentos_total"), d.get("exposicao_atual")]
    known = sum(1 for x in data_fields if safe_float(x) > 0)
    if known >= 3:
        confianca = "ALTA"
    elif known >= 1:
        confianca = "MÉDIA"
    else:
        confianca = "BAIXA"

    # Prudential limit: based on revenue, risk and current exposure.
    if faturamento > 0:
        risk_factor = {"BAIXO": .20, "MÉDIO": .10, "ALTO": 0}[risco]
        technical_limit = faturamento * risk_factor
        if comportamento < 60:
            technical_limit *= 0.70
        if confianca == "BAIXA":
            technical_limit *= 0.50
        limit = max(0, technical_limit - exposicao)
    else:
        limit = 0

    # Cap at R$50k for this generic policy; adjustable in code/policy screen later.
    limit = min(limit, 50000)

    available = max(0, limit)
    entry = 0
    condicao = "À vista"
    decision = "VENDA À VISTA"

    if risco == "BAIXO" and available > 0:
        if pedido <= available:
            condicao = "30/60"
            decision = "APROVAR"
            entry = 0
        elif pedido <= available * 1.20:
            entry = max(0, pedido - available)
            condicao = "Entrada + 30 dias"
            decision = "APROVAR COM CONDIÇÃO"
        else:
            entry = max(0, pedido - available)
            condicao = "Entrada + saldo em 30 dias"
            decision = "REDUZIR LIMITE / EXIGIR ENTRADA"
    elif risco == "MÉDIO" and available > 0:
        if pedido <= available:
            condicao = "À vista + 30 dias"
            decision = "APROVAR COM CONDIÇÃO"
        else:
            entry = max(0, pedido - available)
            condicao = "Entrada + saldo em 30 dias"
            decision = "REDUZIR LIMITE / EXIGIR ENTRADA"
    else:
        entry = pedido
        condicao = "À vista"
        decision = "VENDA À VISTA"

    if hard_stop:
        reasons.append("Bloqueio prudencial: " + "; ".join(hard_stop) + ".")
    if faturamento <= 0:
        reasons.append("Sem faturamento verificável, o limite de crédito não é liberado automaticamente.")

    justificativa = (
        f"Score {score:.1f}/100; risco {risco}; confiança {confianca}. "
        + " ".join(reasons)
        + f" Limite recomendado: {money(limit)}. Condição: {condicao}."
    )

    return {
        "score": score, "risco": risco, "confianca": confianca,
        "limite": limit, "exposicao": exposicao, "limite_disponivel": available,
        "pedido": pedido, "entrada": entry, "condicao": condicao,
        "prazo_medio": 45 if condicao == "30/60" else 30 if "30" in condicao else 0,
        "decisao": decision, "justificativa": justificativa,
        "points": points,
    }


# -----------------------------
# PDF
# -----------------------------
def make_pdf(company: Dict[str, Any], result: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    x = 18 * mm
    y = h - 18 * mm

    def line(text, size=10, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        # crude wrap
        max_chars = 100
        chunks = [text[i:i+max_chars] for i in range(0, len(text), max_chars)] or [""]
        for chunk in chunks:
            c.drawString(x, y, chunk)
            y -= (5 * mm if size <= 10 else 7 * mm)
            if y < 20 * mm:
                c.showPage()
                y = h - 18 * mm

    c.setTitle("Relatório de Análise de Crédito")
    line("RELATÓRIO DE ANÁLISE DE CRÉDITO", 16, True)
    line(f"Gerado em: {datetime.now():%d/%m/%Y %H:%M}")
    line("")
    line("DADOS CADASTRAIS", 12, True)
    line(f"CNPJ: {company.get('cnpj','')}")
    line(f"Razão social: {company.get('razao_social','')}")
    line(f"Nome fantasia: {company.get('nome_fantasia','')}")
    line(f"Situação: {company.get('situacao','')}")
    line(f"Abertura: {company.get('abertura','')}")
    line(f"Capital social: {money(safe_float(company.get('capital_social')))}")
    line(f"Porte: {company.get('porte','')}")
    line(f"CNAE: {company.get('cnae','')}")
    line(f"Endereço: {company.get('endereco','')}, {company.get('cidade','')}/{company.get('uf','')}")
    line("")
    line("RESULTADO", 12, True)
    line(f"Score: {result['score']:.1f}/100")
    line(f"Risco: {result['risco']}")
    line(f"Confiança: {result['confianca']}")
    line(f"Limite recomendado: {money(result['limite'])}")
    line(f"Exposição atual: {money(result['exposicao'])}")
    line(f"Limite disponível: {money(result['limite_disponivel'])}")
    line(f"Pedido: {money(result['pedido'])}")
    line(f"Entrada: {money(result['entrada'])}")
    line(f"Condição: {result['condicao']}")
    line(f"Decisão: {result['decisao']}")
    line("")
    line("JUSTIFICATIVA", 12, True)
    line(result["justificativa"], 9)
    line("")
    line("LIMITAÇÕES", 12, True)
    line("Dados cadastrais não representam, por si só, faturamento, endividamento ou histórico de pagamento.")
    line("O limite é uma recomendação interna e deve ser validado conforme política e dados disponíveis.")
    c.save()
    return buf.getvalue()


# -----------------------------
# UI
# -----------------------------
st.title(APP_TITLE)
st.caption("Motor explicável de crédito: dados cadastrais automáticos + informações financeiras/comportamentais disponíveis.")

menu = st.sidebar.radio("Menu", ["Nova análise", "Histórico", "Política", "Sobre"])

if menu == "Nova análise":
    st.subheader("1. Consulta cadastral")
    cnpj_input = st.text_input("CNPJ", placeholder="00.000.000/0000-00")
    consultar = st.button("CONSULTAR CNPJ", type="primary", use_container_width=True)

    if consultar:
        cnpj = clean_cnpj(cnpj_input)
        if not valid_cnpj(cnpj):
            st.error("CNPJ inválido. Confira os 14 dígitos.")
        else:
            with st.spinner("Consultando fontes cadastrais..."):
                company, errors = consultar_cnpj(cnpj)
            if not company:
                st.error("Não foi possível consultar o CNPJ nas fontes disponíveis.")
                with st.expander("Detalhes técnicos"):
                    for e in errors:
                        st.write(e)
            else:
                st.session_state["company"] = company
                st.success(f"Empresa localizada via {company['fonte_cnpj']}.")

    company = st.session_state.get("company")
    if company:
        st.subheader("2. Dados cadastrais")
        a,b,c = st.columns(3)
        a.metric("Razão social", company["razao_social"] or "—")
        b.metric("Situação", company["situacao"] or "—")
        c.metric("Capital social", money(company["capital_social"]))

        with st.expander("Ver dados cadastrais completos", expanded=True):
            st.write(f"**Nome fantasia:** {company['nome_fantasia'] or '—'}")
            st.write(f"**Abertura:** {company['abertura'] or '—'}")
            st.write(f"**Porte:** {company['porte'] or '—'}")
            st.write(f"**Natureza jurídica:** {company['natureza_juridica'] or '—'}")
            st.write(f"**CNAE:** {company['cnae'] or '—'}")
            st.write(f"**Endereço:** {company['endereco'] or '—'}")
            st.write(f"**Cidade/UF:** {company['cidade'] or '—'} / {company['uf'] or '—'}")
            st.write(f"**Sócios:** {company['socios'] or '—'}")
            st.caption(f"Fonte cadastral: {company['fonte_cnpj']} | Consulta: {company['consultado_em']}")

        st.subheader("3. Dados financeiros e comportamentais")
        st.info("O CNPJ não informa faturamento real. Informe faturamento comprovado ou deixe zero; o sistema não inventará esse dado.")

        col1,col2,col3 = st.columns(3)
        faturamento = col1.number_input("Faturamento mensal comprovado/informado (R$)", min_value=0.0, step=1000.0)
        pedido = col2.number_input("Valor do pedido (R$)", min_value=0.0, step=500.0)
        exposicao = col3.number_input("Exposição atual (R$)", min_value=0.0, step=500.0)

        col1,col2,col3 = st.columns(3)
        compras = col1.number_input("Compras nos últimos 12 meses (R$)", min_value=0.0, step=500.0)
        total = col2.number_input("Total de pagamentos registrados", min_value=0, step=1)
        em_dia = col3.number_input("Pagamentos em dia", min_value=0, step=1)

        col1,col2,col3,col4 = st.columns(4)
        atrasados = col1.number_input("Títulos atrasados", min_value=0, step=1)
        valor_atrasado = col2.number_input("Valor em atraso (R$)", min_value=0.0, step=500.0)
        atraso_medio = col3.number_input("Atraso médio (dias)", min_value=0.0, step=1.0)
        maior_atraso = col4.number_input("Maior atraso (dias)", min_value=0.0, step=1.0)

        col1,col2 = st.columns(2)
        devolucoes = col1.number_input("Devoluções/ocorrências de pagamento", min_value=0, step=1)
        protestos = col2.number_input("Protestos/restrições informados", min_value=0, step=1)

        if st.button("CALCULAR ANÁLISE DE CRÉDITO", type="primary", use_container_width=True):
            d = dict(company)
            d.update({
                "faturamento_mensal": faturamento, "pedido": pedido, "exposicao_atual": exposicao,
                "compras_12m": compras, "pagamentos_total": total, "pagamentos_em_dia": em_dia,
                "titulos_atrasados": atrasados, "valor_atrasado": valor_atrasado,
                "atraso_medio": atraso_medio, "maior_atraso": maior_atraso,
                "devolucoes": devolucoes, "protestos": protestos,
            })
            result = credit_score(d)
            st.session_state["last_company"] = d
            st.session_state["last_result"] = result

            con = db()
            con.execute("""
                INSERT INTO companies
                (cnpj,razao_social,nome_fantasia,situacao,abertura,capital_social,porte,natureza_juridica,cnae,endereco,cidade,uf,socios,fonte_cnpj,consultado_em,
                 faturamento_mensal,faturamento_fonte,compras_12m,exposicao_atual,pedido,pagamentos_total,pagamentos_em_dia,titulos_atrasados,valor_atrasado,atraso_medio,maior_atraso,devolucoes,protestos)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cnpj) DO UPDATE SET
                    razao_social=excluded.razao_social,nome_fantasia=excluded.nome_fantasia,situacao=excluded.situacao,
                    abertura=excluded.abertura,capital_social=excluded.capital_social,porte=excluded.porte,
                    natureza_juridica=excluded.natureza_juridica,cnae=excluded.cnae,endereco=excluded.endereco,
                    cidade=excluded.cidade,uf=excluded.uf,socios=excluded.socios,fonte_cnpj=excluded.fonte_cnpj,
                    consultado_em=excluded.consultado_em,faturamento_mensal=excluded.faturamento_mensal,
                    compras_12m=excluded.compras_12m,exposicao_atual=excluded.exposicao_atual,pedido=excluded.pedido,
                    pagamentos_total=excluded.pagamentos_total,pagamentos_em_dia=excluded.pagamentos_em_dia,
                    titulos_atrasados=excluded.titulos_atrasados,valor_atrasado=excluded.valor_atrasado,
                    atraso_medio=excluded.atraso_medio,maior_atraso=excluded.maior_atraso,
                    devolucoes=excluded.devolucoes,protestos=excluded.protestos
            """, (
                d["cnpj"],d["razao_social"],d["nome_fantasia"],d["situacao"],d["abertura"],d["capital_social"],
                d["porte"],d["natureza_juridica"],d["cnae"],d["endereco"],d["cidade"],d["uf"],d["socios"],
                d["fonte_cnpj"],d["consultado_em"],faturamento,"Informado/validado pelo usuário",compras,
                exposicao,pedido,total,em_dia,atrasados,valor_atrasado,atraso_medio,maior_atraso,devolucoes,protestos
            ))
            con.execute("""
                INSERT INTO analyses
                (cnpj,data,score,risco,confianca,limite,exposicao,limite_disponivel,pedido,entrada,condicao,prazo_medio,decisao,justificativa)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                d["cnpj"], datetime.now().isoformat(timespec="seconds"), result["score"], result["risco"],
                result["confianca"], result["limite"], result["exposicao"], result["limite_disponivel"],
                result["pedido"], result["entrada"], result["condicao"], result["prazo_medio"],
                result["decisao"], result["justificativa"]
            ))
            con.commit()
            con.close()

        result = st.session_state.get("last_result")
        d = st.session_state.get("last_company")
        if result and d:
            st.subheader("4. Resultado")
            cols = st.columns(5)
            cols[0].metric("Score", f"{result['score']:.1f}/100")
            cols[1].metric("Risco", result["risco"])
            cols[2].metric("Confiança", result["confianca"])
            cols[3].metric("Limite", money(result["limite"]))
            cols[4].metric("Limite disponível", money(result["limite_disponivel"]))

            st.write(f"**Decisão:** {result['decisao']}")
            st.write(f"**Condição recomendada:** {result['condicao']}")
            st.write(f"**Entrada:** {money(result['entrada'])}")
            st.write("### Justificativa")
            st.info(result["justificativa"])

            with st.expander("Composição do score"):
                labels = {
                    "cadastro":"Cadastro","tempo":"Tempo de atividade","capacidade":"Capacidade",
                    "comportamento":"Comportamento","exposicao":"Exposição",
                    "relacionamento":"Relacionamento","externo":"Dados externos"
                }
                score_rows = []
                for k,v in result["points"].items():
                    score_rows.append({"Critério":labels[k],"Peso":WEIGHTS[k],"Nota do critério":round(v,1),
                                       "Pontos":round(v*WEIGHTS[k]/100,1)})
                st.dataframe(pd.DataFrame(score_rows), use_container_width=True, hide_index=True)

            pdf_bytes = make_pdf(d, result)
            st.download_button(
                "BAIXAR RELATÓRIO PDF", data=pdf_bytes,
                file_name=f"credito_{d['cnpj']}.pdf",
                mime="application/pdf",
                use_container_width=True
            )

elif menu == "Histórico":
    st.subheader("Histórico de análises")
    con = db()
    df = pd.read_sql_query("SELECT * FROM analyses ORDER BY id DESC", con)
    con.close()
    if df.empty:
        st.info("Nenhuma análise registrada.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

elif menu == "Política":
    st.subheader("Política de crédito atual")
    st.write("Pesos do score:")
    st.dataframe(pd.DataFrame([{"Critério":k.title(),"Peso":v} for k,v in WEIGHTS.items()]), hide_index=True, use_container_width=True)
    st.write("Faixas:")
    st.write("- BAIXO: score ≥ 80")
    st.write("- MÉDIO: score entre 60 e 79,9")
    st.write("- ALTO: score < 60")
    st.write("Bloqueios prudenciais: CNPJ não ativo; maior atraso > 60 dias; ou atraso superior a 20% do faturamento mensal informado.")
    st.write("Condições possíveis: À vista; À vista + 30 dias; Entrada + 30 dias; 30/60; Entrada + saldo em 30 dias.")
    st.warning("Essas regras são um modelo interno explicável, não uma garantia de inadimplência futura.")

else:
    st.subheader("Sobre o sistema")
    st.write("Versão 4.0 — análise de crédito empresarial explicável.")
    st.write("Consulta cadastral automática por CNPJ e motor de crédito separado de dados externos.")
    st.write("Faturamento, endividamento e histórico de pagamento não são inferidos como fatos quando não estão disponíveis.")
    st.caption("Fontes cadastrais: BrasilAPI / Minha Receita e CNPJ.ws Pública. As APIs possuem limites e podem sofrer indisponibilidade.")
