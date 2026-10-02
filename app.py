
import io
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

APP_TITLE = "Fratelli Crédito 5.0"
DB_PATH = "fratelli_credito.db"
TIMEOUT = 12
ASSET_DIR = Path("assets")
LOGO_PATH = ASSET_DIR / "fratelli_logo.png"

st.set_page_config(page_title=APP_TITLE, page_icon="🍾", layout="wide")


# ============================================================
# Helpers
# ============================================================
def clean_cnpj(v: str) -> str:
    return re.sub(r"\D", "", str(v or ""))


def format_cnpj(v: str) -> str:
    n = clean_cnpj(v)
    if len(n) != 14:
        return v or ""
    return f"{n[:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:]}"


def valid_cnpj(cnpj: str) -> bool:
    cnpj = clean_cnpj(cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    nums = [int(x) for x in cnpj]
    w1 = [5,4,3,2,9,8,7,6,5,4,3,2]
    w2 = [6,5,4,3,2,9,8,7,6,5,4,3,2]
    d1 = sum(a*b for a,b in zip(nums[:12], w1)) % 11
    d1 = 0 if d1 < 2 else 11-d1
    d2 = sum(a*b for a,b in zip(nums[:12] + [d1], w2)) % 11
    d2 = 0 if d2 < 2 else 11-d2
    return d1 == nums[12] and d2 == nums[13]


def safe_float(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def money(v: float) -> str:
    return f"R$ {safe_float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
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


def first_nonempty(*vals):
    for v in vals:
        if v is not None and str(v).strip() not in ("", "None", "nan"):
            return v
    return ""


def num(v: Any) -> float:
    return safe_float(v)


# ============================================================
# Database
# ============================================================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        cnpj TEXT NOT NULL,
        razao_social TEXT,
        fantasia TEXT,
        situacao TEXT,
        abertura TEXT,
        porte TEXT,
        segmento TEXT,
        faturamento REAL,
        faturamento_fonte TEXT,
        pedido REAL,
        exposicao REAL,
        compras_12m REAL,
        total_pagamentos INTEGER,
        pagamentos_dia INTEGER,
        titulos_atrasados INTEGER,
        valor_atraso REAL,
        atraso_medio REAL,
        maior_atraso REAL,
        ocorrencias INTEGER,
        restricoes INTEGER,
        score REAL,
        risco TEXT,
        confianca REAL,
        limite REAL,
        disponivel REAL,
        prazo TEXT,
        entrada REAL,
        decisao TEXT,
        justificativa TEXT,
        dados_json TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        cnpj TEXT PRIMARY KEY,
        razao_social TEXT,
        fantasia TEXT,
        situacao TEXT,
        abertura TEXT,
        porte TEXT,
        segmento TEXT,
        capital REAL,
        ultima_consulta TEXT,
        dados_json TEXT
    )
    """)
    con.commit()
    con.close()


init_db()


def save_client(data: Dict[str, Any]):
    con = db()
    con.execute("""
    INSERT INTO clients
    (cnpj, razao_social, fantasia, situacao, abertura, porte, segmento, capital, ultima_consulta, dados_json)
    VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(cnpj) DO UPDATE SET
      razao_social=excluded.razao_social,
      fantasia=excluded.fantasia,
      situacao=excluded.situacao,
      abertura=excluded.abertura,
      porte=excluded.porte,
      segmento=excluded.segmento,
      capital=excluded.capital,
      ultima_consulta=excluded.ultima_consulta,
      dados_json=excluded.dados_json
    """, (
        data.get("cnpj"), data.get("razao_social"), data.get("fantasia"),
        data.get("situacao"), data.get("abertura"), data.get("porte"),
        data.get("segmento"), data.get("capital", 0),
        datetime.now().isoformat(timespec="seconds"),
        data.get("_raw_json", "")
    ))
    con.commit()
    con.close()


def save_analysis(data: Dict[str, Any]):
    con = db()
    con.execute("""
    INSERT INTO analyses (
      created_at, cnpj, razao_social, fantasia, situacao, abertura, porte, segmento,
      faturamento, faturamento_fonte, pedido, exposicao, compras_12m,
      total_pagamentos, pagamentos_dia, titulos_atrasados, valor_atraso,
      atraso_medio, maior_atraso, ocorrencias, restricoes, score, risco, confianca,
      limite, disponivel, prazo, entrada, decisao, justificativa, dados_json
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(timespec="seconds"),
        data.get("cnpj"), data.get("razao_social"), data.get("fantasia"),
        data.get("situacao"), data.get("abertura"), data.get("porte"),
        data.get("segmento"), data.get("faturamento", 0),
        data.get("faturamento_fonte"), data.get("pedido", 0),
        data.get("exposicao", 0), data.get("compras_12m", 0),
        data.get("total_pagamentos", 0), data.get("pagamentos_dia", 0),
        data.get("titulos_atrasados", 0), data.get("valor_atraso", 0),
        data.get("atraso_medio", 0), data.get("maior_atraso", 0),
        data.get("ocorrencias", 0), data.get("restricoes", 0),
        data.get("score", 0), data.get("risco"), data.get("confianca", 0),
        data.get("limite", 0), data.get("disponivel", 0), data.get("prazo"),
        data.get("entrada", 0), data.get("decisao"),
        data.get("justificativa"), data.get("_raw_json", "")
    ))
    con.commit()
    con.close()


# ============================================================
# CNPJ providers
# ============================================================
def normalize_provider(raw: Dict[str, Any], source: str) -> Dict[str, Any]:
    est = raw.get("estabelecimento") or {}
    if source == "cnpjws":
        abertura = est.get("data_inicio_atividade")
        situacao = est.get("situacao_cadastral")
        fantasia = est.get("nome_fantasia")
        cidade = (est.get("cidade") or {}).get("nome")
        uf = (est.get("estado") or {}).get("sigla")
        principal = (est.get("atividade_principal") or {}).get("descricao")
        porte = (raw.get("porte") or {}).get("descricao")
        natureza = (raw.get("natureza_juridica") or {}).get("descricao")
        capital = raw.get("capital_social")
        socios = raw.get("socios") or []
        cnpj = est.get("cnpj") or raw.get("cnpj")
        razao = raw.get("razao_social")
    else:
        abertura = first_nonempty(raw.get("data_inicio_atividade"), raw.get("data_situacao_cadastral"))
        situacao = raw.get("descricao_situacao_cadastral") or raw.get("situacao_cadastral")
        fantasia = raw.get("nome_fantasia")
        cidade = raw.get("municipio")
        uf = raw.get("uf")
        principal = raw.get("cnae_fiscal_descricao")
        porte = raw.get("porte")
        natureza = raw.get("natureza_juridica")
        capital = raw.get("capital_social")
        socios = raw.get("qsa") or []
        cnpj = raw.get("cnpj")
        razao = raw.get("razao_social")

    try:
        capital_num = float(str(capital).replace(".", "").replace(",", "."))
    except Exception:
        capital_num = safe_float(capital)

    return {
        "cnpj": clean_cnpj(cnpj),
        "razao_social": razao or "",
        "fantasia": fantasia or "",
        "situacao": situacao or "",
        "abertura": abertura or "",
        "porte": porte or "",
        "capital": capital_num,
        "cidade": cidade or "",
        "uf": uf or "",
        "principal": principal or "",
        "natureza": natureza or "",
        "socios": socios,
        "source": source,
        "_raw": raw
    }


def query_cnpj(cnpj: str, token: str = "") -> Tuple[Optional[Dict[str, Any]], List[str]]:
    cnpj = clean_cnpj(cnpj)
    errors = []
    headers = {"User-Agent": "FratelliCredito/5.0"}

    urls = [
        ("BrasilAPI", f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"),
        ("CNPJ.ws pública", f"https://publica.cnpj.ws/cnpj/{cnpj}")
    ]

    if token.strip():
        urls.insert(0, ("CNPJ.ws comercial", f"https://comercial.cnpj.ws/cnpj/{cnpj}"))

    for source, url in urls:
        try:
            h = dict(headers)
            if source == "CNPJ.ws comercial":
                h["x_api_token"] = token.strip()
            r = requests.get(url, headers=h, timeout=TIMEOUT)
            if r.status_code == 200:
                return normalize_provider(r.json(), "cnpjws" if "CNPJ.ws" in source else "brasilapi"), errors
            errors.append(f"{source}: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}")
    return None, errors


# ============================================================
# Credit engine
# ============================================================
SEGMENTS = {
    "Bar": {"factor": 0.06, "standard_days": 21},
    "Restaurante": {"factor": 0.07, "standard_days": 28},
    "Distribuidor": {"factor": 0.10, "standard_days": 28},
    "Supermercado": {"factor": 0.08, "standard_days": 28},
    "Empório": {"factor": 0.07, "standard_days": 28},
    "Hotel": {"factor": 0.07, "standard_days": 28},
    "Outro": {"factor": 0.05, "standard_days": 14},
}

WEIGHTS = {
    "capacidade": 20,
    "pagamentos": 20,
    "exposicao": 15,
    "restricoes": 15,
    "atividade": 10,
    "comportamento": 10,
    "relacionamento": 5,
    "qualidade": 5,
}


def score_capacity(faturamento, pedido, exposicao):
    if faturamento > 0:
        ratio = (pedido + exposicao) / faturamento
        if ratio <= 0.02: return 100
        if ratio <= 0.05: return 90
        if ratio <= 0.10: return 75
        if ratio <= 0.20: return 55
        if ratio <= 0.35: return 35
        return 15
    return None


def score_payments(total, ontime, avg_delay, max_delay):
    if total <= 0:
        return None
    pct = max(0, min(1, ontime / total))
    s = pct * 100
    if avg_delay > 0:
        s -= min(30, avg_delay * 1.5)
    if max_delay > 60:
        s -= 35
    elif max_delay > 30:
        s -= 20
    elif max_delay > 15:
        s -= 10
    return max(0, min(100, s))


def score_exposure(faturamento, exposicao):
    if faturamento <= 0:
        return None
    ratio = exposicao / faturamento
    if ratio <= 0.02: return 100
    if ratio <= 0.05: return 90
    if ratio <= 0.10: return 75
    if ratio <= 0.20: return 55
    if ratio <= 0.35: return 35
    return 10


def score_restrictions(restricoes, ocorrencias):
    if restricoes > 0:
        return max(0, 25 - min(20, restricoes * 5))
    if ocorrencias > 0:
        return max(45, 85 - min(40, ocorrencias * 8))
    return 100


def score_activity(anos):
    if anos >= 10: return 100
    if anos >= 5: return 90
    if anos >= 3: return 80
    if anos >= 2: return 70
    if anos >= 1: return 55
    if anos > 0: return 40
    return None


def score_behavior(titulos, valor_atraso, maior_atraso):
    if titulos <= 0 and valor_atraso <= 0:
        return None
    if maior_atraso <= 0 and valor_atraso <= 0:
        return 100
    if maior_atraso <= 7: return 90
    if maior_atraso <= 15: return 75
    if maior_atraso <= 30: return 60
    if maior_atraso <= 60: return 40
    return 15


def score_relationship(compras_12m):
    if compras_12m <= 0:
        return None
    if compras_12m >= 100000: return 100
    if compras_12m >= 50000: return 90
    if compras_12m >= 25000: return 80
    if compras_12m >= 10000: return 65
    return 50


def quality_score(data):
    available = 0
    total = 0
    checks = [
        ("razao_social", bool(data.get("razao_social"))),
        ("situacao", bool(data.get("situacao"))),
        ("abertura", bool(data.get("abertura"))),
        ("porte", bool(data.get("porte"))),
        ("faturamento", data.get("faturamento", 0) > 0),
        ("pagamentos", data.get("total_pagamentos", 0) > 0),
        ("exposicao", data.get("faturamento", 0) > 0),
        ("relacionamento", data.get("compras_12m", 0) > 0),
    ]
    for _, ok in checks:
        total += 1
        available += int(ok)
    return available / total * 100


def weighted_score(data):
    parts = {
        "capacidade": score_capacity(data["faturamento"], data["pedido"], data["exposicao"]),
        "pagamentos": score_payments(data["total_pagamentos"], data["pagamentos_dia"], data["atraso_medio"], data["maior_atraso"]),
        "exposicao": score_exposure(data["faturamento"], data["exposicao"]),
        "restricoes": score_restrictions(data["restricoes"], data["ocorrencias"]),
        "atividade": score_activity(data["anos"]),
        "comportamento": score_behavior(data["titulos_atrasados"], data["valor_atraso"], data["maior_atraso"]),
        "relacionamento": score_relationship(data["compras_12m"]),
        "qualidade": quality_score(data),
    }

    weighted = 0
    used_weight = 0
    for k, w in WEIGHTS.items():
        val = parts[k]
        if val is not None:
            weighted += val * w
            used_weight += w
    score = weighted / used_weight if used_weight else 0
    confidence = min(100, (used_weight / 100) * 100)

    # Missing financial data should reduce confidence, not automatically punish the company.
    return round(max(0, min(100, score)), 1), round(confidence, 1), parts


def risk_and_decision(data, score, confidence):
    situacao = str(data.get("situacao", "")).upper()
    restricoes = data["restricoes"]
    maior_atraso = data["maior_atraso"]
    pedido = data["pedido"]

    hard_stop = False
    hard_reason = []

    if situacao and not any(x in situacao for x in ["ATIVA", "REGULAR"]):
        hard_stop = True
        hard_reason.append("situação cadastral não está ativa/regular")
    if restricoes > 0:
        hard_stop = True
        hard_reason.append(f"{restricoes} restrição(ões) informada(s)")
    if maior_atraso > 90:
        hard_stop = True
        hard_reason.append(f"maior atraso de {maior_atraso:.0f} dias")

    if hard_stop:
        risco = "ALTO"
    elif score >= 80:
        risco = "BAIXO"
    elif score >= 60:
        risco = "MÉDIO"
    else:
        risco = "ALTO"

    segment = SEGMENTS.get(data["segmento"], SEGMENTS["Outro"])
    faturamento = data["faturamento"]
    exposicao = data["exposicao"]

    if hard_stop:
        limite = 0
    elif faturamento > 0:
        base = faturamento * segment["factor"]
        if risco == "BAIXO":
            limite = min(30000, max(3000, base))
        elif risco == "MÉDIO":
            limite = min(12000, max(1500, base * 0.60))
        else:
            limite = 0
    else:
        # Without verified revenue, use a conservative internal-history-only ceiling.
        if risco == "BAIXO" and data["compras_12m"] > 0:
            limite = min(5000, max(1000, data["compras_12m"] * 0.05))
        else:
            limite = 0

    limite = max(0, limite)
    disponivel = max(0, limite - exposicao)

    # Payment terms
    if risco == "BAIXO" and confidence >= 75:
        if pedido <= disponivel * 0.50:
            prazo = "30/60"
        elif pedido <= disponivel:
            prazo = "À vista + 30"
        else:
            prazo = "Entrada + 30"
    elif risco == "MÉDIO" and confidence >= 60:
        prazo = "À vista + 30" if pedido <= disponivel else "Entrada + 30"
    elif risco == "MÉDIO":
        prazo = "À vista"
    else:
        prazo = "À vista"

    if pedido <= 0:
        decisao = "ANÁLISE SEM PEDIDO"
        entrada = 0
    elif pedido <= disponivel and prazo != "À vista":
        decisao = "APROVAR"
        entrada = 0
    elif pedido <= disponivel and prazo == "À vista":
        decisao = "APROVAR À VISTA"
        entrada = pedido
    elif disponivel > 0:
        decisao = "APROVAR COM ENTRADA"
        entrada = max(0, pedido - disponivel)
    else:
        decisao = "VENDA À VISTA"
        entrada = pedido

    reasons = []
    if data["situacao"]:
        reasons.append(f"situação cadastral: {data['situacao']}")
    if data["anos"] > 0:
        reasons.append(f"{data['anos']:.1f} anos de atividade")
    if data["faturamento"] > 0:
        ratio = (pedido + exposicao) / data["faturamento"] * 100
        reasons.append(f"pedido + exposição equivalem a {ratio:.1f}% do faturamento mensal")
    else:
        reasons.append("faturamento não comprovado na fonte cadastral; não foi tratado como zero")
    if data["total_pagamentos"] > 0:
        pct = data["pagamentos_dia"] / data["total_pagamentos"] * 100
        reasons.append(f"{pct:.1f}% dos pagamentos registrados em dia")
    if data["maior_atraso"] > 0:
        reasons.append(f"maior atraso informado: {data['maior_atraso']:.0f} dias")
    if data["restricoes"] == 0:
        reasons.append("nenhuma restrição informada")
    if hard_reason:
        reasons.extend(hard_reason)

    justificativa = "; ".join(reasons) + "."
    return risco, limite, disponivel, prazo, entrada, decisao, justificativa


def run_analysis(data):
    score, confidence, parts = weighted_score(data)
    risco, limite, disponivel, prazo, entrada, decisao, justificativa = risk_and_decision(data, score, confidence)
    return {
        **data,
        "score": score,
        "confianca": confidence,
        "parts": parts,
        "risco": risco,
        "limite": limite,
        "disponivel": disponivel,
        "prazo": prazo,
        "entrada": entrada,
        "decisao": decisao,
        "justificativa": justificativa,
    }


# ============================================================
# PDF
# ============================================================
def pdf_report(row: Dict[str, Any]) -> bytes:
    bio = io.BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)
    w, h = A4
    x = 18 * mm
    y = h - 20 * mm

    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, y, "FRATELLI CRÉDITO 5.0")
    y -= 10 * mm
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, y, "Relatório de análise de crédito")
    y -= 9 * mm

    lines = [
        f"CNPJ: {format_cnpj(row.get('cnpj',''))}",
        f"Razão social: {row.get('razao_social','')}",
        f"Nome fantasia: {row.get('fantasia','')}",
        f"Situação: {row.get('situacao','')}",
        f"Segmento: {row.get('segmento','')}",
        f"Score: {row.get('score',0):.1f}/100",
        f"Risco: {row.get('risco','')}",
        f"Confiança da análise: {row.get('confianca',0):.1f}%",
        f"Faturamento mensal informado/comprovado: {money(row.get('faturamento',0))}",
        f"Fonte do faturamento: {row.get('faturamento_fonte','')}",
        f"Exposição atual: {money(row.get('exposicao',0))}",
        f"Limite recomendado: {money(row.get('limite',0))}",
        f"Crédito disponível: {money(row.get('disponivel',0))}",
        f"Pedido: {money(row.get('pedido',0))}",
        f"Condição recomendada: {row.get('prazo','')}",
        f"Entrada: {money(row.get('entrada',0))}",
        f"Decisão: {row.get('decisao','')}",
    ]
    c.setFont("Helvetica", 9.5)
    for line in lines:
        if y < 35*mm:
            c.showPage()
            y = h - 20*mm
            c.setFont("Helvetica", 9.5)
        c.drawString(x, y, line[:115])
        y -= 6 * mm

    y -= 3 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, "Justificativa")
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    text = str(row.get("justificativa",""))
    words = text.split()
    current = ""
    for word in words:
        if len(current + " " + word) > 105:
            c.drawString(x, y, current)
            y -= 5 * mm
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        c.drawString(x, y, current)

    y -= 10 * mm
    c.setFont("Helvetica-Oblique", 7.5)
    c.drawString(x, y, "Documento interno de apoio à decisão. Não substitui análise humana, contrato ou fonte de crédito autorizada.")
    c.save()
    return bio.getvalue()


# ============================================================
# UI
# ============================================================
def show_brand():
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=180)
    else:
        st.markdown("### 🍾 FRATELLI")
        st.caption("Sistema de análise de crédito empresarial")


def sidebar():
    show_brand()
    st.divider()
    page = st.radio("Menu", [
        "Nova análise",
        "Histórico",
        "Clientes",
        "Política de crédito",
        "Configuração CNPJ"
    ])
    st.divider()
    st.caption("Fratelli Crédito 5.0")
    st.caption("Dados desconhecidos não são tratados como dados negativos.")
    return page


def page_new_analysis():
    st.title("Análise de crédito")
    st.write("Informe o CNPJ. O sistema busca os dados cadastrais automaticamente e depois combina-os com os dados financeiros/comportamentais disponíveis.")

    cnpj = st.text_input("CNPJ", placeholder="00.000.000/0000-00", key="cnpj_input")
    col1, col2 = st.columns([1, 4])
    with col1:
        consult = st.button("🔎 CONSULTAR CNPJ", type="primary", use_container_width=True)

    if consult:
        if not valid_cnpj(cnpj):
            st.error("CNPJ inválido. Verifique os 14 dígitos.")
            return
        with st.spinner("Consultando fontes cadastrais..."):
            data, errors = query_cnpj(cnpj, st.session_state.get("cnpj_token", ""))
        if data:
            st.session_state["company"] = data
            save_client({
                **data,
                "segmento": "Outro",
                "_raw_json": str(data.get("_raw", {}))
            })
            st.success(f"Empresa encontrada via {data['source']}.")
            if errors:
                st.caption("Uma ou mais fontes alternativas não responderam; a consulta foi concluída pela fonte disponível.")
        else:
            st.error("Não foi possível localizar o CNPJ nas fontes configuradas.")
            for e in errors:
                st.caption(e)
            return

    company = st.session_state.get("company")
    if not company:
        st.info("Digite um CNPJ e clique em CONSULTAR CNPJ.")
        return

    st.subheader("1. Dados cadastrais")
    c1, c2, c3 = st.columns(3)
    c1.metric("Razão social", company.get("razao_social") or "Não informado")
    c2.metric("Nome fantasia", company.get("fantasia") or "Não informado")
    c3.metric("Situação", company.get("situacao") or "Não informado")

    c1, c2, c3 = st.columns(3)
    c1.metric("Abertura", company.get("abertura") or "Não informado")
    c2.metric("Porte", company.get("porte") or "Não informado")
    c3.metric("Capital social", money(company.get("capital", 0)))

    st.caption(f"Atividade principal: {company.get('principal') or 'Não informado'}")
    st.caption(f"Localização: {company.get('cidade') or '—'} / {company.get('uf') or '—'}")
    if company.get("socios"):
        st.caption(f"Sócios retornados pela fonte: {len(company['socios'])}")

    st.subheader("2. Perfil comercial")
    segmento = st.selectbox("Segmento", list(SEGMENTS.keys()), index=list(SEGMENTS.keys()).index(
        st.session_state.get("segmento_sel", "Outro")
    ))
    st.session_state["segmento_sel"] = segmento

    st.subheader("3. Dados financeiros e comportamentais")
    st.info("Os dados abaixo não são inventados pelo CNPJ. Se você não possuir uma fonte financeira autorizada, deixe o campo desconhecido/zero. O motor reduzirá a confiança, mas não penalizará automaticamente a empresa como se tivesse desempenho ruim.")

    c1, c2, c3 = st.columns(3)
    faturamento = c1.number_input("Faturamento mensal comprovado/informado (R$)", min_value=0.0, step=1000.0)
    pedido = c2.number_input("Valor do pedido (R$)", min_value=0.0, step=500.0)
    exposicao = c3.number_input("Exposição atual Fratelli (R$)", min_value=0.0, step=500.0)

    fonte_fat = st.selectbox("Fonte do faturamento", [
        "Não informado",
        "Documento financeiro fornecido pelo cliente",
        "Informação declarada pelo cliente",
        "Fonte financeira/bureau autorizado",
        "Histórico interno Fratelli"
    ])

    c1, c2, c3 = st.columns(3)
    compras_12m = c1.number_input("Compras Fratelli nos últimos 12 meses (R$)", min_value=0.0, step=1000.0)
    total_pagamentos = c2.number_input("Total de pagamentos registrados", min_value=0, step=1)
    pagamentos_dia = c3.number_input("Pagamentos em dia", min_value=0, step=1)

    c1, c2, c3, c4 = st.columns(4)
    titulos_atrasados = c1.number_input("Títulos atrasados", min_value=0, step=1)
    valor_atraso = c2.number_input("Valor em atraso (R$)", min_value=0.0, step=500.0)
    atraso_medio = c3.number_input("Atraso médio (dias)", min_value=0.0, step=1.0)
    maior_atraso = c4.number_input("Maior atraso (dias)", min_value=0.0, step=1.0)

    c1, c2 = st.columns(2)
    ocorrencias = c1.number_input("Devoluções/ocorrências de pagamento", min_value=0, step=1)
    restricoes = c2.number_input("Restrições/protestos confirmados", min_value=0, step=1)

    anos = years_active(parse_date(company.get("abertura")))

    data = {
        "cnpj": clean_cnpj(company["cnpj"]),
        "razao_social": company.get("razao_social",""),
        "fantasia": company.get("fantasia",""),
        "situacao": company.get("situacao",""),
        "abertura": company.get("abertura",""),
        "porte": company.get("porte",""),
        "segmento": segmento,
        "capital": company.get("capital",0),
        "faturamento": faturamento,
        "faturamento_fonte": fonte_fat,
        "pedido": pedido,
        "exposicao": exposicao,
        "compras_12m": compras_12m,
        "total_pagamentos": total_pagamentos,
        "pagamentos_dia": min(pagamentos_dia, total_pagamentos),
        "titulos_atrasados": titulos_atrasados,
        "valor_atraso": valor_atraso,
        "atraso_medio": atraso_medio,
        "maior_atraso": maior_atraso,
        "ocorrencias": ocorrencias,
        "restricoes": restricoes,
        "anos": anos,
        "_raw_json": str(company.get("_raw", {}))
    }

    if st.button("⚡ EXECUTAR ANÁLISE COMPLETA", type="primary", use_container_width=True):
        result = run_analysis(data)
        st.session_state["last_result"] = result
        save_analysis(result)

    result = st.session_state.get("last_result")
    if not result:
        return

    st.subheader("4. Resultado da análise")
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Score", f"{result['score']:.1f}/100")
    c2.metric("Risco", result["risco"])
    c3.metric("Confiança", f"{result['confianca']:.0f}%")
    c4.metric("Limite", money(result["limite"]))
    c5.metric("Disponível", money(result["disponivel"]))

    st.success(f"**DECISÃO: {result['decisao']}**")
    st.info(f"**CONDIÇÃO RECOMENDADA: {result['prazo']}**  |  **ENTRADA: {money(result['entrada'])}**")
    st.write("### Justificativa")
    st.write(result["justificativa"])

    st.write("### Composição do score")
    labels = {
        "capacidade":"Capacidade financeira",
        "pagamentos":"Histórico de pagamentos",
        "exposicao":"Exposição",
        "restricoes":"Restrições",
        "atividade":"Tempo de atividade",
        "comportamento":"Comportamento",
        "relacionamento":"Relacionamento Fratelli",
        "qualidade":"Qualidade dos dados",
    }
    comp = []
    for k,v in result["parts"].items():
        comp.append({"Critério": labels[k], "Nota": "N/D" if v is None else round(v,1), "Peso": WEIGHTS[k]})
    st.dataframe(pd.DataFrame(comp), use_container_width=True, hide_index=True)

    if result["faturamento"] <= 0:
        st.warning("⚠️ O limite está conservador porque não há faturamento comprovado. O sistema não transforma ausência de informação em faturamento zero para penalizar a empresa; apenas reduz a confiança e limita o crédito quando necessário.")

    pdf = pdf_report(result)
    st.download_button(
        "📄 BAIXAR RELATÓRIO PDF",
        data=pdf,
        file_name=f"credito_{clean_cnpj(result['cnpj'])}.pdf",
        mime="application/pdf",
        use_container_width=True
    )


def page_history():
    st.title("Histórico de análises")
    con = db()
    df = pd.read_sql_query("SELECT created_at, cnpj, razao_social, score, risco, confianca, limite, disponivel, prazo, decisao FROM analyses ORDER BY id DESC", con)
    con.close()
    if df.empty:
        st.info("Nenhuma análise registrada.")
        return
    df["cnpj"] = df["cnpj"].apply(format_cnpj)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("⬇️ Exportar CSV", df.to_csv(index=False).encode("utf-8-sig"), "historico_credito.csv", "text/csv")


def page_clients():
    st.title("Clientes")
    con = db()
    df = pd.read_sql_query("SELECT * FROM clients ORDER BY ultima_consulta DESC", con)
    con.close()
    if df.empty:
        st.info("Nenhum cliente consultado.")
        return
    df["cnpj"] = df["cnpj"].apply(format_cnpj)
    st.dataframe(df[["cnpj","razao_social","fantasia","situacao","porte","segmento","capital","ultima_consulta"]], use_container_width=True, hide_index=True)


def page_policy():
    st.title("Política de crédito")
    st.write("Os pesos são visíveis e auditáveis. A política padrão foi desenhada para não transformar dado ausente em penalização automática.")
    st.dataframe(pd.DataFrame([
        {"Critério": "Capacidade financeira", "Peso": "20%"},
        {"Critério": "Histórico de pagamentos", "Peso": "20%"},
        {"Critério": "Exposição", "Peso": "15%"},
        {"Critério": "Restrições/protestos", "Peso": "15%"},
        {"Critério": "Tempo de atividade", "Peso": "10%"},
        {"Critério": "Comportamento", "Peso": "10%"},
        {"Critério": "Relacionamento Fratelli", "Peso": "5%"},
        {"Critério": "Qualidade dos dados", "Peso": "5%"},
    ]), use_container_width=True, hide_index=True)

    st.write("### Regras principais")
    st.markdown("""
- **Score ≥ 80:** risco baixo.
- **Score 60–79,9:** risco médio.
- **Score < 60:** risco alto.
- Restrição confirmada ou situação cadastral não regular pode bloquear crédito.
- Atraso superior a 90 dias é tratado como gatilho forte de bloqueio.
- Sem faturamento comprovado, o sistema não inventa receita: reduz a confiança e aplica limite conservador.
- O limite considera faturamento, segmento, risco e exposição já existente.
- O prazo é definido pelo risco, confiança e espaço disponível no limite.
""")
    st.caption("Essas regras são uma política interna inicial; devem ser validadas pela Fratelli antes de serem usadas como única base de concessão de crédito.")


def page_config():
    st.title("Configuração CNPJ")
    st.write("O sistema usa fontes públicas como fallback e permite configurar um token de fonte comercial autorizada.")
    token = st.text_input("Token CNPJ.ws comercial (opcional)", type="password", value=st.session_state.get("cnpj_token",""))
    if st.button("Salvar token"):
        st.session_state["cnpj_token"] = token
        st.success("Token mantido apenas na sessão atual. Para produção, use secrets do Streamlit.")
    st.markdown("""
### Fontes cadastradas
1. BrasilAPI — consulta cadastral.
2. CNPJ.ws pública — fallback.
3. CNPJ.ws comercial — opcional, com token.

**Importante:** nenhuma dessas fontes cadastrais deve ser tratada como prova de faturamento real. Para faturamento, score externo, protestos e comportamento de crédito, use uma fonte financeira/bureau autorizada e integre o resultado ao motor.
""")


def main():
    page = sidebar()
    if page == "Nova análise":
        page_new_analysis()
    elif page == "Histórico":
        page_history()
    elif page == "Clientes":
        page_clients()
    elif page == "Política de crédito":
        page_policy()
    else:
        page_config()


main()
