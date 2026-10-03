import sqlite3
import json
import math
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

APP_DIR = Path(__file__).parent
DB = APP_DIR / "fratelli_credito.db"
LOGO = APP_DIR / "assets" / "fratelli_logo.png"

st.set_page_config(page_title="Fratelli", page_icon=str(LOGO) if LOGO.exists() else "💳", layout="wide")

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


PUBLIC_SOURCES = [
    ("CNPJ.BIZ", "https://cnpj.biz/{cnpj}"),
    ("CNPJ.ai", "https://cnpj.ai/{cnpj}"),
    ("BuscaSim", "https://buscasim.com.br/cnpj/{cnpj}-COMERCIAL-GAMA"),
    ("Serasa Empresas", "https://empresas.serasaexperian.com.br/consulta-gratis/LILIAN-KARLEY-SANTOS-GAMA-LTDA-{cnpj}"),
    ("CNPJCheck", "https://cnpjcheck.com.br/empresa/lilian-karley-santos-gama-ltda-comercial-gama-{cnpj}"),
    ("CadastroEmpresa", "https://cadastroempresa.com.br/fornecedor/lilian-karley-santos-gama-{cnpj}"),
]

def fetch_public_page(url, source):
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent":"Mozilla/5.0 (compatible; FratelliCredito/5.1)"})
        if r.status_code >= 400:
            return {"source":source,"url":url,"ok":False,"status":r.status_code,"text":""}
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script","style","noscript"]): tag.decompose()
        text = " ".join(soup.stripped_strings)
        return {"source":source,"url":url,"ok":True,"status":r.status_code,"text":text[:250000]}
    except Exception as e:
        return {"source":source,"url":url,"ok":False,"status":None,"text":"","error":str(e)}

def first_match(text, patterns):
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            return re.sub(r'\s+',' ',m.group(1)).strip(' |:')
    return None

def parse_public_cnpj_page(page):
    text=page.get("text","")
    fields={}
    fields["Razão social"]=first_match(text,[r"Razão Social\s*[:|]?\s*([^|]{3,100}?)(?=\s+Nome Fantasia|\s+Data da Abertura|\s+CNPJ)",r"Razão social\s*[:|]?\s*([^|]{3,100}?)(?=\s+Nome fantasia)"])
    fields["Nome fantasia"]=first_match(text,[r"Nome Fantasia\s*[:|]?\s*([^|]{2,100}?)(?=\s+Data da Abertura|\s+Porte|\s+CNPJ)",r"Nome fantasia\s*[:|]?\s*([^|]{2,100}?)(?=\s+Data)"])
    fields["Data de abertura"]=first_match(text,[r"Data da Abertura\s*[:|]?\s*(\d{2}/\d{2}/\d{4})",r"Data de abertura\s*[:|]?\s*(\d{2}/\d{2}/\d{4})"])
    fields["Situação cadastral"]=first_match(text,[r"Situação Cadastral\s*[:|]?\s*([A-ZÁÉÍÓÚÇÃÕ ]{4,30})(?=\s+Data|\s+Capital|\s+Natureza)",r"Situação\s*[:|]?\s*(ATIVA|INATIVA|BAIXADA|SUSPENSA|INAPTA)"])
    fields["Natureza jurídica"]=first_match(text,[r"Natureza Jurídica\s*[:|]?\s*([^|]{5,100}?)(?=\s+Opção|\s+Capital|\s+Tipo)",r"natureza jurídica\s+([^|]{5,100}?)(?=\s+Matriz)"])
    fields["Capital social"]=first_match(text,[r"Capital Social\s*[:|]?\s*(R\$\s*[0-9\.\,]+)",r"capital social\s+(R\$\s*[0-9\.\,]+)"])
    fields["Porte"]=first_match(text,[r"Porte\s*[:|]?\s*([^|]{2,40})(?=\s+Natureza|\s+Capital|\s+Tipo)"])
    fields["CNAE principal"]=first_match(text,[r"Principal\s*[:|]?\s*([0-9]{2}\.?[0-9]{2}-?[0-9]/?[0-9]{2})\s*-\s*([^|]{5,120}?)(?=\s+Atividades|\s+Descritores|\s+Sobre)"])
    if fields["CNAE principal"] is None:
        fields["CNAE principal"]=first_match(text,[r"CNAE principal\s*[:|]?\s*([0-9\.\-/]+\s*-\s*[^|]{5,120})"])
    fields["Endereço"]=first_match(text,[r"Logradouro\s*[:|]?\s*([^|]{5,150}?)(?=\s+Bairro|\s+CEP|\s+Município)"])
    fields["Bairro"]=first_match(text,[r"Bairro\s*[:|]?\s*([^|]{2,80})(?=\s+CEP|\s+Município|\s+Estado)"])
    fields["Município/UF"]=first_match(text,[r"Município\s*[:|]?\s*([^|]{2,100}?)(?=\s+Estado|\s+Para correspondência|\s+Atividades)",r"Município\s*/\s*UF\s*[:|]?\s*([^|]{2,100})"])
    fields["CEP"]=first_match(text,[r"CEP\s*[:|]?\s*(\d{5}-\d{3})"])
    fields["Telefone"]=first_match(text,[r"Telefone\s*[:|]?\s*(\(\d{2}\)\s*[0-9\-\* ]{7,20})"])
    fields={k:v for k,v in fields.items() if v}
    partners=[]
    for m in re.finditer(r"(?:Sócio-Administrador|Sócio\s+Administrador|Sócio)\s+([A-ZÁÉÍÓÚÇÃÕ][A-ZÁÉÍÓÚÇÃÕ ]{5,80})", text):
        n=m.group(1).strip()
        if len(n.split())>=2 and n not in partners: partners.append(n)
    # common explicit name pattern on sources
    if not partners:
        for pat in [r"Nome\s+\|\s+Tipo\s+\|\s+Qualificação.*?\n?([A-ZÁÉÍÓÚÇÃÕ][A-ZÁÉÍÓÚÇÃÕ ]{5,80})", r"Quadro Societário.*?([A-ZÁÉÍÓÚÇÃÕ][A-ZÁÉÍÓÚÇÃÕ ]{5,80})"]:
            m=re.search(pat,text,re.I|re.S)
            if m:
                n=m.group(1).strip()
                if len(n.split())>=2: partners.append(n)
    return fields, list(dict.fromkeys(partners))

def build_public_dossier(cnpj):
    c=clean_cnpj(cnpj)
    if len(c)!=14: return {"ok":False,"message":"Informe um CNPJ com 14 dígitos.","sources":[],"fields":{},"partners":[],"conflicts":[]}
    pages=[]
    # The URLs are public pages, not APIs. Some sources can block automated access; failures remain visible.
    for source,template in PUBLIC_SOURCES:
        url=template.format(cnpj=c)
        # Generic source URLs are safer when the name is unknown; use only the CNPJ-specific endpoints here.
        if source in {"BuscaSim","Serasa Empresas","CNPJCheck","CadastroEmpresa"}:
            continue
        pages.append(fetch_public_page(url,source))
    # Public search pages are used only to surface discoverable evidence.
    # They are not treated as authoritative proof of debt, process, or identity.
    search_queries=[c]
    for q in search_queries:
        url="https://html.duckduckgo.com/html/?q="+quote(q+" empresa CNPJ processos")
        pages.append(fetch_public_page(url,"Busca pública — CNPJ/processos"))
    source_rows=[]; values={}; partners=[]
    for p in pages:
        if p.get("ok"):
            fields,ps=parse_public_cnpj_page(p)
            p["fields"]=fields; p["partners"]=ps
            for k,v in fields.items(): values.setdefault(k,[]).append((p["source"],v))
            partners.extend(ps)
        source_rows.append(p)
    conflicts=[]; consolidated={}
    for k,vals in values.items():
        norm={re.sub(r'\s+',' ',v.strip().upper()) for _,v in vals}
        if len(norm)>1:
            conflicts.append({"campo":k,"valores":vals})
        # majority value; ties use first source. We never hide conflicting values.
        counts={}
        for src,v in vals: counts[v]=counts.get(v,0)+1
        consolidated[k]=max(vals,key=lambda x:counts[x[1]])[1]
    partners=list(dict.fromkeys([p for p in partners if p and len(p.split())>=2]))
    # Search public web for each identified partner name. Results are shown as leads only.
    for partner in partners[:5]:
        qurl="https://html.duckduckgo.com/html/?q="+quote('"'+partner+'" processos empresa')
        pp=fetch_public_page(qurl,"Busca pública — sócio/administrador")
        pp["query_name"]=partner
        source_rows.append(pp)
    return {"ok":True,"queried_at":datetime.now().isoformat(timespec="seconds"),"sources":source_rows,"fields":consolidated,"field_sources":values,"partners":partners,"conflicts":conflicts}

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
    con.execute("""CREATE TABLE IF NOT EXISTS movimentos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER,
        data TEXT,
        tipo TEXT,
        valor REAL,
        vencimento TEXT,
        pagamento TEXT,
        dias_atraso INTEGER DEFAULT 0,
        observacao TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS auditoria(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT,
        usuario TEXT,
        acao TEXT,
        entidade TEXT,
        entidade_id INTEGER,
        antes TEXT,
        depois TEXT,
        motivo TEXT
    )""")
    # Migração leve: mantém bancos 4.x existentes sem exigir API ou reinstalação.
    cols = {r[1] for r in con.execute("PRAGMA table_info(movimentos)").fetchall()}
    if "data_pagamento" not in cols:
        con.execute("ALTER TABLE movimentos ADD COLUMN data_pagamento TEXT")
    if "documento" not in cols:
        con.execute("ALTER TABLE movimentos ADD COLUMN documento TEXT")
    if "categoria" not in cols:
        con.execute("ALTER TABLE movimentos ADD COLUMN categoria TEXT")
    con.commit()
    return con

def clean_cnpj(x):
    return "".join(c for c in str(x) if c.isdigit())

def money(x):
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def cnpj_validate_local(cnpj):
    """Valida CNPJ somente localmente (formato + dígitos verificadores).
    Não consulta Receita Federal, Serasa, APIs ou qualquer fonte externa.
    Situação cadastral e dados da empresa devem ser confirmados pelo operador.
    """
    digits = clean_cnpj(cnpj)
    if len(digits) != 14:
        return None, "CNPJ inválido: informe os 14 dígitos."
    if len(set(digits)) == 1:
        return None, "CNPJ inválido: sequência repetida."
    nums = [int(x) for x in digits]
    w1 = [5,4,3,2,9,8,7,6,5,4,3,2]
    r1 = sum(n*w for n,w in zip(nums[:12], w1)) % 11
    d1 = 0 if r1 < 2 else 11-r1
    w2 = [6,5,4,3,2,9,8,7,6,5,4,3,2]
    r2 = sum(n*w for n,w in zip(nums[:13], w2)) % 11
    d2 = 0 if r2 < 2 else 11-r2
    if nums[12] != d1 or nums[13] != d2:
        return None, "CNPJ inválido: dígitos verificadores não conferem."
    formatted = f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return {"cnpj": digits, "cnpj_formatted": formatted}, "CNPJ válido matematicamente. Nenhuma consulta externa foi realizada. Confirme manualmente razão social, situação cadastral e demais dados."

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
    if faturamento is None or faturamento <= 0:
        return None
    if fonte == "Não informado":
        return None
    if pedido <= 0:
        base = 75 if fonte == "Declaração do cliente" else 90
        return base
    ratio = pedido / faturamento
    base = 100 if ratio <= .03 else 90 if ratio <= .05 else 75 if ratio <= .08 else 55 if ratio <= .12 else 30
    if fonte == "Declaração do cliente":
        base = min(base, 65)
    return base


def score_pagamento(compras, atrasos, maior_atraso, pontualidade):
    vals=[]
    if atrasos is not None or maior_atraso is not None or pontualidade is not None:
        if atrasos is not None:
            vals.append(100 if atrasos==0 else 70 if atrasos<=2 else 40 if atrasos<=5 else 15)
        if maior_atraso is not None:
            vals.append(100 if maior_atraso==0 else 80 if maior_atraso<=7 else 60 if maior_atraso<=30 else 30 if maior_atraso<=90 else 0)
        if pontualidade is not None:
            vals.append(max(0,min(100,pontualidade)))
    if not vals:
        return None
    return round(sum(vals)/len(vals),1)


def score_exposicao(faturamento, aberto, limite_atual=0):
    if faturamento is None or faturamento <= 0 or aberto is None:
        return None
    # O limite disponível é derivado do limite aprovado; o limite atual não é
    # somado à exposição, evitando dupla contagem.
    exposicao = max(0.0, aberto) / faturamento
    return 100 if exposicao <= .03 else 85 if exposicao <= .06 else 70 if exposicao <= .10 else 45 if exposicao <= .15 else 20

def score_operacional(restricoes, fonte_restricoes):
    # Aqui, somente informação efetivamente fornecida é considerada.
    if restricoes is None:
        return None
    return 100 if restricoes == 0 else 55 if restricoes <= 2 else 20

MISSING_SCORE_FACTOR = 0.25

def calc_score(items):
    """Calcula score com penalização conservadora por ausência de evidência.

    Critério informado: usa a pontuação efetivamente calculada.
    Critério sem informação suficiente: recebe 25% da pontuação máxima (25/100).
    Cobertura continua sendo reportada separadamente e não é usada para
    renormalizar o score, evitando que poucos dados favoráveis produzam um
    resultado artificialmente alto.
    """
    total_weight = sum(WEIGHTS.values())
    used = sum(WEIGHTS[k] for k,v in items.items() if v is not None)
    missing = [k for k,v in items.items() if v is None]
    score = sum(
        WEIGHTS[k] * (MISSING_SCORE_FACTOR * 100 if v is None else v)
        for k,v in items.items()
    ) / total_weight
    coverage = round(used/total_weight*100, 1)
    return round(score, 1), coverage, missing

def risk(score, situacao, maior_atraso):
    if score is None:
        return "NÃO CLASSIFICADO"
    if situacao and ("ATIVA" not in situacao.upper() and "REGULAR" not in situacao.upper()):
        return "ELEVADO"
    if maior_atraso is not None and maior_atraso > 90:
        return "ELEVADO"
    if score < 25: return "MUITO ELEVADO"
    if score < 35: return "ELEVADO"
    if score < 45: return "MODERADO-ALTO"
    if score < 55: return "MODERADO"
    return "CONTROLADO"

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
    if risco == "MUITO ELEVADO":
        return "À vista", 0, "CRÉDITO NÃO RECOMENDADO"
    if risco == "ELEVADO":
        return "Entrada + 15 dias", round(pedido*.50, 2), "APROVAÇÃO COM CONDIÇÃO"
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



def audit(con, action, entity, entity_id=None, before="", after="", reason=""):
    con.execute("""INSERT INTO auditoria(data,usuario,acao,entidade,entidade_id,antes,depois,motivo)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (datetime.now().isoformat(), "operador", action, entity, entity_id,
                 str(before), str(after), str(reason)))
    con.commit()

def client_movements(con, client_id):
    return pd.read_sql_query(
        "SELECT * FROM movimentos WHERE cliente_id=? ORDER BY data DESC, id DESC",
        con, params=(client_id,))

def behavior_from_real_history(df):
    """Métricas de comportamento usando somente títulos de venda/compra.
    Pagamentos não viram artificialmente novas contas nem aumentam exposição.
    """
    if df.empty:
        return {"has_history": False, "purchases": 0.0, "late_count": None,
                "max_late": None, "avg_late": None, "on_time": None,
                "open": 0.0, "overdue_open": 0.0, "paid_titles": 0,
                "total_titles": 0, "last_sale": None, "last_payment": None,
                "max_purchase": 0.0}
    work=df.copy()
    work["valor"]=pd.to_numeric(work["valor"], errors="coerce").fillna(0.0)
    work["dias_atraso"]=pd.to_numeric(work["dias_atraso"], errors="coerce").fillna(0).astype(int)
    titles=work[work["tipo"].isin(["Venda","Compra"])].copy()
    purchases=float(titles["valor"].sum()) if not titles.empty else 0.0
    total_titles=len(titles)
    paid=titles[titles["pagamento"].fillna("").eq("Pago")]
    unpaid=titles[~titles["pagamento"].fillna("").eq("Pago")]
    late=titles[titles["dias_atraso"]>0]
    on_time=(len(paid[paid["dias_atraso"]<=0])/len(paid)*100) if len(paid) else None
    open_amount=float(unpaid["valor"].sum()) if not unpaid.empty else 0.0
    overdue_open=float(unpaid.loc[unpaid["dias_atraso"]>0,"valor"].sum()) if not unpaid.empty else 0.0
    dates=pd.to_datetime(titles["data"], errors="coerce")
    pay_dates=pd.to_datetime(work.loc[work["tipo"].eq("Pagamento"),"data"], errors="coerce")
    return {
        "has_history": True, "purchases": purchases,
        "late_count": int(len(late)) if total_titles else None,
        "max_late": int(titles["dias_atraso"].max()) if total_titles else None,
        "avg_late": float(late["dias_atraso"].mean()) if len(late) else 0.0,
        "on_time": on_time, "open": open_amount, "overdue_open": overdue_open,
        "paid_titles": int(len(paid)), "total_titles": int(total_titles),
        "last_sale": dates.max().date().isoformat() if dates.notna().any() else None,
        "last_payment": pay_dates.max().date().isoformat() if pay_dates.notna().any() else None,
        "max_purchase": float(titles["valor"].max()) if total_titles else 0.0,
    }

def payment_evidence_score(real, manual_confirmed=False):
    """Score de comportamento: quantidade, pontualidade e severidade dos atrasos.
    Retorna None quando não há evidência suficiente.
    """
    if not real.get("has_history") and not manual_confirmed:
        return None
    total=real.get("total_titles",0)
    on=real.get("on_time")
    maxlate=real.get("max_late")
    if total < 3 and on is None and maxlate is None:
        return None
    components=[]
    if total >= 3:
        components.append(min(100, 60 + min(total,12)*3))
    if on is not None:
        components.append(max(0,min(100,on)))
    if maxlate is not None:
        components.append(100 if maxlate==0 else 85 if maxlate<=7 else 70 if maxlate<=30 else 45 if maxlate<=60 else 20 if maxlate<=90 else 0)
    return round(sum(components)/len(components),1) if components else None

def dso_estimate(real, monthly_revenue):
    """DSO aproximado, somente quando há receita e exposição reais suficientes."""
    rev=float(monthly_revenue or 0)
    if rev<=0 or not real.get("has_history"):
        return None
    open_amount=float(real.get("open",0) or 0)
    return round(open_amount/rev*30,1)

def risk_trend(real):
    """Sinal de tendência, não soma pontos ao score. Usa apenas o histórico interno."""
    if not real.get("has_history") or real.get("total_titles",0)<3:
        return "N/D"
    if (real.get("max_late") or 0)>90 or (real.get("on_time") is not None and real["on_time"]<80):
        return "PIORA / ALERTA"
    if real.get("on_time") is not None and real["on_time"]>=95 and (real.get("max_late") or 0)<=7:
        return "ESTÁVEL / FAVORÁVEL"
    return "ESTÁVEL"

def contradiction_checks(data, faturamento, pedido, aberto, limite_atual, real):
    alerts=[]
    if faturamento>0 and pedido>faturamento:
        alerts.append("Pedido superior ao faturamento mensal informado.")
    if limite_atual>0 and aberto>limite_atual*1.05:
        alerts.append("Exposição aberta acima do limite atual informado.")
    if real.get("has_history") and real.get("purchases",0)>0 and faturamento>0 and real["purchases"]>faturamento*24:
        alerts.append("Histórico acumulado muito acima do faturamento mensal informado; verificar período/fonte.")
    if real.get("has_history") and real.get("total_titles",0)>=3 and real.get("on_time") is not None and real["on_time"]>100:
        alerts.append("Percentual de pontualidade inconsistente.")
    return alerts


def score_billing_policy(score, requested_order):
    """Commercial credit policy requested by the business owner.
    Boundaries: <25 no sale; 25-<35 up to 1,500; 35-<45 up to 5,000;
    45-<55 up to 10,000; 55-70 up to 20,000; >70 follows the customer's request.
    """
    if score is None:
        return {"band":"SEM SCORE", "ceiling":0.0, "action":"PENDENTE — score indisponível."}
    score=float(score)
    requested=max(0.0,float(requested_order or 0))
    if score < 25:
        return {"band":"< 25", "ceiling":0.0, "action":"NÃO VENDER — risco muito elevado."}
    if score < 35:
        return {"band":"25 a <35", "ceiling":1500.0, "action":"Até R$ 1.500,00."}
    if score < 45:
        return {"band":"35 a <45", "ceiling":5000.0, "action":"Até R$ 5.000,00."}
    if score < 55:
        return {"band":"45 a <55", "ceiling":10000.0, "action":"Até R$ 10.000,00."}
    if score <= 70:
        return {"band":"55 a 70", "ceiling":20000.0, "action":"Até R$ 20.000,00."}
    return {"band":"> 70", "ceiling":requested, "action":"Valor solicitado pelo cliente, sujeito aos controles de exposição/capacidade."}


def compare_request_to_policy(score, requested, policy_ceiling, available_financial, gate):
    requested=max(0.0,float(requested or 0))
    policy_ceiling=max(0.0,float(policy_ceiling or 0))
    available_financial=max(0.0,float(available_financial or 0))
    if score is None:
        return {"status":"PENDENTE", "approved":0.0, "gap":requested, "reason":"Sem score."}
    if score < 25:
        return {"status":"NÃO APROVADO", "approved":0.0, "gap":requested, "reason":"Score abaixo de 25: política determina não vender."}
    if gate in ("BLOQUEADO", "PENDENTE", "REVISÃO MANUAL"):
        return {"status":gate, "approved":0.0, "gap":requested, "reason":"Controle de decisão impede aprovação automática: " + gate}
    if requested <= 0:
        return {"status":"SEM PEDIDO", "approved":0.0, "gap":0.0, "reason":"Informe o valor ou quantidade solicitada pelo cliente."}
    # Score policy defines the commercial ceiling; exposure/capacity can only reduce it.
    effective=max(0.0,min(policy_ceiling, available_financial))
    approved=min(requested,effective)
    gap=max(0.0,requested-approved)
    if approved >= requested:
        return {"status":"APROVAÇÃO INTEGRAL", "approved":approved, "gap":0.0,
                "reason":"Pedido dentro do limite permitido pelo score e pelos controles financeiros."}
    if approved > 0:
        return {"status":"APROVAÇÃO PARCIAL", "approved":approved, "gap":gap,
                "reason":"Pedido excede o limite permitido; aprovar somente o valor conservador calculado."}
    return {"status":"NÃO APROVADO", "approved":0.0, "gap":requested,
            "reason":"Não há limite disponível para este pedido."}


def box_request_value(boxes, price_montanhas, price_desertos, price_canions):
    """Calculates request value by Fratelli cases of 9 bottles."""
    boxes=max(0,int(boxes or 0))
    if boxes<=0:
        return {"boxes":0,"bottles":0,"value":0.0,"breakdown":{}}
    # Equal-mix default is transparent and editable by the operator through unit quantities below.
    return {"boxes":boxes,"bottles":boxes*9,
            "value":0.0,
            "breakdown":{}}

def limit_engine(score, risk_level, monthly_revenue, segment, history_12m,
                 on_time, open_amount, current_limit, requested_order):
    """
    Motor 4.4:
    - calcula limite recomendado a partir da capacidade;
    - aplica risco e comportamento como moderadores;
    - NÃO subtrai o limite atual, evitando dupla contagem;
    - desconta somente a exposição efetivamente em aberto;
    - calcula utilização antes/depois do pedido.
    """
    if score is None or risk_level == "NÃO CLASSIFICADO":
        return {
            "recommended": 0.0, "available": 0.0, "post_order": 0.0,
            "utilization_before": None, "utilization_after": None,
            "reason": "Sem limite automático por ausência de score."
        }

    monthly_revenue = float(monthly_revenue or 0)
    history_12m = float(history_12m or 0)
    open_amount = max(0.0, float(open_amount or 0))
    requested_order = max(0.0, float(requested_order or 0))

    if monthly_revenue <= 0:
        if history_12m > 0 and (on_time is None or on_time >= 90):
            base = min(5000.0, history_12m * 0.50)
        else:
            return {
                "recommended": 0.0, "available": 0.0, "post_order": 0.0,
                "utilization_before": None, "utilization_after": None,
                "reason": "Sem faturamento/capacidade financeira suficiente."
            }
    else:
        factor = SEGMENT_FACTORS.get(segment, .05)
        base = monthly_revenue * factor

    if risk_level == "MODERADO":
        base = min(base, 12000.0)
    else:
        base = min(base, 30000.0)

    if history_12m > 0 and on_time is not None:
        if on_time >= 97:
            behavior_factor = 1.00
        elif on_time >= 90:
            behavior_factor = 0.90
        elif on_time >= 80:
            behavior_factor = 0.70
        else:
            behavior_factor = 0.40
        base *= behavior_factor

    recommended = max(0.0, round(base, -2))
    available = max(0.0, round(recommended - open_amount, -2))
    utilization_before = None if recommended <= 0 else round(open_amount / recommended * 100, 1)
    post_exposure = open_amount + requested_order
    utilization_after = None if recommended <= 0 else round(post_exposure / recommended * 100, 1)
    post_order = max(0.0, round(recommended - post_exposure, -2))

    reason = (
        "Capacidade financeira + risco + comportamento + exposição. "
        "O limite atual é apenas informativo; somente a exposição em aberto reduz o limite disponível."
    )
    return {
        "recommended": recommended,
        "available": available,
        "post_order": post_order,
        "utilization_before": utilization_before,
        "utilization_after": utilization_after,
        "reason": reason
    }


def order_decision(available, requested_order, gate, risk_level):
    """Decisão financeira do pedido, separada da condição comercial."""
    requested_order = max(0.0, float(requested_order or 0))
    available = max(0.0, float(available or 0))

    if gate in ("BLOQUEADO", "PENDENTE", "REVISÃO MANUAL"):
        return gate, 0.0
    if requested_order <= 0:
        return "SEM PEDIDO INFORMADO", 0.0
    if requested_order <= available:
        return "APROVAÇÃO INTEGRAL", requested_order
    if risk_level == "CONTROLADO" and available > 0:
        return "APROVAÇÃO PARCIAL", available
    return "NÃO APROVADO PARA ESTE PEDIDO", 0.0


def pdf_report(data, path):
    if not REPORTLAB_OK:
        return False
    c = canvas.Canvas(str(path), pagesize=A4)
    w,h = A4
    y = h-42
    if LOGO.exists():
        try:
            c.drawImage(ImageReader(str(LOGO)), 45, y-45, width=150, height=40, preserveAspectRatio=True, mask="auto")
            y -= 58
        except Exception:
            pass
    c.setFont("Helvetica-Bold", 18)
    c.drawString(45,y,"Fratelli")
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

# Identidade Fratelli no menu lateral
if LOGO.exists():
    import base64
    _logo_b64 = base64.b64encode(LOGO.read_bytes()).decode()
    st.sidebar.markdown(f'<div style="text-align:center;padding:4px 0 16px"><img src="data:image/png;base64,{_logo_b64}" style="max-width:170px;max-height:72px;object-fit:contain"></div>', unsafe_allow_html=True)

st.markdown("""
<style>
.fratelli-header {
    display:flex; align-items:center; gap:28px;
    padding:8px 0 18px 0;
    border-bottom:1px solid rgba(49,51,63,.12);
    margin-bottom:18px;
}
.fratelli-header img {
    width:220px; max-height:78px; object-fit:contain; object-position:left center;
}
.fratelli-title {
    font-size:2.15rem; font-weight:750; line-height:1.05; color:#30313d;
}
.fratelli-subtitle {
    margin-top:6px; font-size:1rem; color:#7a7d86;
}
@media (max-width: 700px) {
  .fratelli-header {flex-direction:column; align-items:flex-start; gap:10px;}
  .fratelli-header img {width:190px;}
}
</style>
""", unsafe_allow_html=True)

if LOGO.exists():
    st.markdown(
        f'<div class="fratelli-header"><img src="data:image/png;base64,{__import__("base64").b64encode(LOGO.read_bytes()).decode()}"><div><div class="fratelli-title">Fratelli</div><div class="fratelli-subtitle">Análise e decisão de crédito B2B — evidências públicas, dados internos e decisão explicável.</div></div></div>',
        unsafe_allow_html=True
    )
else:
    st.title("Fratelli")
    st.caption("Análise e decisão de crédito B2B — evidências públicas, dados internos e decisão explicável.")

menu = st.sidebar.radio("Menu", ["Nova análise", "Histórico real", "Histórico", "Clientes", "Auditoria", "Metodologia"])

if menu == "Nova análise":
    st.subheader("1. Cadastro da empresa")
    c1,c2,c3 = st.columns(3)
    with c1:
        cnpj = st.text_input("CNPJ", help="O sistema apenas valida o número localmente. Não busca dados cadastrais externos.")
        if st.button("Validar CNPJ"):
            data,msg = cnpj_validate_local(cnpj)
            st.session_state["cnpj_data"] = data
            (st.success if data else st.error)(msg)
        elif cnpj and len(clean_cnpj(cnpj)) == 14:
            st.caption("Validação disponível: clique em **Validar CNPJ**. Esta função é local e não consulta nenhuma API.")
    # Pesquisa pública do cliente: traz os dados para dentro do sistema, não apenas links.
    with st.expander("🔎 Dossiê público do cliente", expanded=True):
        st.caption("Pesquisa em páginas públicas disponíveis na internet. Não usa API de crédito. Cada fonte é exibida separadamente e divergências não são ocultadas.")
        if st.button("Pesquisar dados públicos agora", key="pesquisar_publico_51"):
            with st.spinner("Consultando fontes públicas..."):
                st.session_state["public_dossier"] = build_public_dossier(cnpj)
        dossier=st.session_state.get("public_dossier")
        if dossier and dossier.get("ok"):
            st.success(f"Pesquisa concluída em {dossier.get('queried_at','')}. Fontes respondidas: {sum(1 for x in dossier['sources'] if x.get('ok'))}/{len(dossier['sources'])}.")
            flds=dossier.get("fields",{})
            if flds:
                rows=[]
                for k,v in flds.items():
                    srcs="; ".join(src for src,_ in dossier.get("field_sources",{}).get(k,[]))
                    rows.append({"Campo":k,"Valor consolidado":v,"Fontes":srcs})
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            if dossier.get("conflicts"):
                st.warning("DIVERGÊNCIAS ENTRE FONTES — o sistema não escolhe silenciosamente uma versão.")
                for cfl in dossier["conflicts"]:
                    st.write(f"**{cfl['campo']}**")
                    st.dataframe(pd.DataFrame([{"Fonte":s,"Valor":v} for s,v in cfl["valores"]]), use_container_width=True, hide_index=True)
            if dossier.get("partners"):
                st.markdown("### Sócios/administradores encontrados")
                for partner in dossier["partners"]:
                    st.write(f"• {partner}")
                    st.caption("Pesquisa de nome deve ser tratada como evidência pública potencial; homônimos exigem confirmação de identidade.")
                    purl="https://www.jusbrasil.com.br/busca?q="+quote(partner)
                    st.markdown(f"Pesquisa pública do nome: {purl}")
            st.markdown("### Fontes consultadas")
            for src in dossier["sources"]:
                status="OK" if src.get("ok") else "SEM RESPOSTA"
                st.write(f"**{src['source']} — {status}** | {src['url']}")
                if src.get("ok"):
                    snippet=src.get("text","")[:500]
                    st.caption(snippet+('…' if len(src.get('text',''))>500 else ''))
            st.info("Importante: ausência de resultado em uma página pública não significa ausência de dívida, protesto ou processo. Consultas privadas/creditícias detalhadas só entram como evidência se houver fonte autorizada ou documento fornecido pelo usuário.")
        elif dossier and not dossier.get("ok"):
            st.error(dossier.get("message","Não foi possível concluir a pesquisa."))

    data = st.session_state.get("cnpj_data") or {}
    dossier=st.session_state.get("public_dossier") or {}
    public_fields=dossier.get("fields",{}) if dossier else {}
    with c2:
        razao = st.text_input("Razão social", value=data.get("razao_social", public_fields.get("Razão social", "")))
        fantasia = st.text_input("Nome fantasia", value=data.get("nome_fantasia", public_fields.get("Nome fantasia", "")))
    with c3:
        segmento = st.selectbox("Segmento", list(SEGMENT_FACTORS.keys()))
        situacao_manual = st.selectbox("Situação cadastral confirmada", ["Não informado","ATIVA/REGULAR","SUSPENSA/INAPTA","Outra situação"])
        anos_atividade_manual = st.number_input("Anos de atividade confirmados", min_value=0.0, step=1.0, value=0.0)
        cadastro_confirmado = st.checkbox("Cadastro/documentação conferidos pelo operador", value=False)

    st.subheader("2. Capacidade financeira e solicitação do cliente")
    a,b,c,d = st.columns(4)
    with a: faturamento = st.number_input("Faturamento mensal (R$)", min_value=0.0, step=1000.0)
    with b: fonte = st.selectbox("Fonte do faturamento", ["Não informado","Documento financeiro","Fonte financeira autorizada","Declaração do cliente"])
    with c: pedido = st.number_input("Valor solicitado pelo cliente (R$)", min_value=0.0, step=100.0, help="Valor efetivamente pedido pelo cliente. Será comparado com o score, o limite de política e a exposição.")
    with d: prazo_pedido = st.selectbox("Prazo solicitado", ["À vista","30 dias","30/60 dias","60 dias","90 dias"])

    with st.expander("Pedido por caixas — 9 garrafas por caixa"):
        st.caption("Informe a quantidade de caixas solicitada. O sistema calcula o valor e compara automaticamente com a política de crédito.")
        q1,q2,q3,q4 = st.columns(4)
        with q1: caixas_montanhas = st.number_input("Montanhas — caixas", min_value=0, step=1, key="cx_montanhas")
        with q2: caixas_desertos = st.number_input("Desertos — caixas", min_value=0, step=1, key="cx_desertos")
        with q3: caixas_canions = st.number_input("Cânions — caixas", min_value=0, step=1, key="cx_canions")
        with q4: desconto_pedido = st.number_input("Desconto comercial (%)", min_value=0.0, max_value=100.0, value=0.0, step=0.5, key="desc_pedido")
        preco_montanhas = st.number_input("Preço unitário Montanhas (R$)", min_value=0.0, value=166.20, step=1.0, key="p_mont")
        preco_desertos = st.number_input("Preço unitário Desertos (R$)", min_value=0.0, value=162.20, step=1.0, key="p_des")
        preco_canions = st.number_input("Preço unitário Cânions (R$)", min_value=0.0, value=175.20, step=1.0, key="p_can")
        total_caixas = int(caixas_montanhas + caixas_desertos + caixas_canions)
        total_garrafas = total_caixas * 9
        valor_caixas_bruto = (caixas_montanhas*9*preco_montanhas + caixas_desertos*9*preco_desertos + caixas_canions*9*preco_canions)
        valor_caixas = valor_caixas_bruto * (1 - desconto_pedido/100)
        st.write(f"**Solicitação por caixas:** {total_caixas} caixa(s) / {total_garrafas} garrafa(s) / **{money(valor_caixas)}**")
        usar_caixas = st.checkbox("Usar o valor calculado pelas caixas como pedido solicitado", value=False, key="usar_caixas")
        if usar_caixas:
            pedido = float(valor_caixas)


    st.subheader("3. Histórico real e exposição")
    cnpj_clean = clean_cnpj(cnpj)
    existing_client = None
    if cnpj_clean:
        row = con.execute("SELECT id FROM clientes WHERE cnpj=?", (cnpj_clean,)).fetchone()
        existing_client = row[0] if row else None

    real_df = client_movements(con, existing_client) if existing_client else pd.DataFrame()
    real = behavior_from_real_history(real_df)
    use_real = st.checkbox("Usar histórico real registrado no sistema", value=not real_df.empty)

    a,b,c,d = st.columns(4)
    with a:
        compras_manual = st.number_input("Compras 12 meses (R$)", min_value=0.0, step=1000.0)
    with b:
        aberto_manual = st.number_input("Valor em aberto (R$)", min_value=0.0, step=500.0)
    with c:
        limite_atual = st.number_input("Limite atual (R$)", min_value=0.0, step=500.0)
    with d:
        pontualidade_manual = st.number_input("% no prazo", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
        confirmar_pontualidade_manual = st.checkbox("Confirmar % manual")

    if use_real and not real_df.empty:
        compras_12m = real["purchases"]
        aberto = real["open"]
        pontualidade = real["on_time"]
        atrasos = real["late_count"]
        maior_atraso = real["max_late"]
        st.success(f"Histórico real encontrado: {len(real_df)} movimento(s).")
        if real.get("avg_late") is not None:
            st.caption(f"Atraso médio dos títulos atrasados: {real['avg_late']:.1f} dias | Maior compra registrada: {money(real['max_purchase'])} | Tendência: {risk_trend(real)}")
    else:
        compras_12m = compras_manual
        aberto = aberto_manual
        pontualidade = pontualidade_manual
        atrasos = None
        maior_atraso = None

    e,f = st.columns(2)
    with e:
        restricoes = st.number_input("Restrições/protestos confirmados", min_value=0, step=1)
    with f:
        tem_restricoes = st.checkbox("Consulta de restrições confirmada")

    st.caption("Se um dado não for conhecido, deixe-o sem informação/zero apenas quando o zero for um fato confirmado. Para análises rigorosas, diferencie 'zero confirmado' de 'não informado'.")

    # Explicit checkbox avoids confusing unknown with zero.
    tem_historico = use_real or st.checkbox("Tenho histórico manual de pagamentos")
    tem_pontualidade = (pontualidade is not None) and (use_real or confirmar_pontualidade_manual)
    payment_component = (payment_evidence_score(real, manual_confirmed=tem_historico)
                         if use_real else score_pagamento(
                             compras_12m if tem_historico and compras_12m > 0 else None,
                             atrasos if tem_historico and atrasos is not None else None,
                             maior_atraso if tem_historico and maior_atraso is not None else None,
                             pontualidade if tem_pontualidade and pontualidade is not None else None))
    itens = {
        "Cadastro e estabilidade": score_cadastro(
            (data.get("descricao_situacao_cadastral") or (situacao_manual if situacao_manual != "Não informado" else None)),
            (anos_atividade_manual if anos_atividade_manual > 0 else ((datetime.now()-datetime.strptime(data["data_inicio_atividade"], "%Y-%m-%d")).days/365.25 if data.get("data_inicio_atividade") else None))
        ),
        "Capacidade financeira": score_capacidade(
            faturamento if faturamento > 0 else None,
            fonte, pedido
        ),
        "Histórico de pagamento": payment_component,
        "Exposição": score_exposicao(
            faturamento if faturamento > 0 else None,
            aberto, limite_atual
        ),
        "Comportamento operacional": score_operacional(
            restricoes if tem_restricoes else None, fonte
        ),
    }
    score,cobertura,criterios_sem_evidencia = calc_score(itens)

    verified_sources = 0
    if cadastro_confirmado: verified_sources += 1
    if faturamento > 0 and fonte in ("Documento financeiro","Fonte financeira autorizada"): verified_sources += 1
    if tem_historico: verified_sources += 1
    if tem_restricoes: verified_sources += 1
    public_ok = bool(dossier.get("ok")) if dossier else False
    public_sources_ok = sum(1 for x in dossier.get("sources",[]) if x.get("ok")) if dossier else 0
    public_conflicts = len(dossier.get("conflicts",[])) if dossier else 0
    if public_ok and public_sources_ok >= 2: verified_sources += 1
    quality = data_quality_label(cobertura, verified_sources)
    inconsistencias = contradiction_checks(data, faturamento, pedido, aberto, limite_atual, real)

    situacao = data.get("descricao_situacao_cadastral","") or (situacao_manual if situacao_manual != "Não informado" else "")
    risco = risk(score,situacao,maior_atraso if tem_historico else None)
    gate, gate_reason = decision_gate(
        cnpj_ok=bool(data) and cadastro_confirmado,
        score=score,
        coverage=cobertura,
        restrictions_confirmed=tem_restricoes,
        active_restrictions=(restricoes if tem_restricoes else None),
        largest_delay=(maior_atraso if tem_historico else None),
    )
    if inconsistencias and gate == "OK":
        gate = "REVISÃO MANUAL"
        gate_reason = "Inconsistências internas detectadas: " + " | ".join(inconsistencias)

    # Motor financeiro interno: capacidade/exposição.
    motor = limit_engine(
        score, risco, faturamento, segmento, compras_12m,
        pontualidade if pontualidade is not None else None,
        aberto, limite_atual, pedido
    )
    capacidade_limite = motor["recommended"]
    capacidade_disponivel = motor["available"]

    # Nova política comercial explícita por faixa de score.
    policy = score_billing_policy(score, pedido)
    # Para >70 a política usa o pedido como referência; para faixas inferiores é teto fixo.
    if score is not None and score > 70:
        policy_ceiling = float(pedido or 0)
    else:
        policy_ceiling = float(policy["ceiling"])
    # Quando não há capacidade financeira comprovável, a política de score continua visível,
    # mas não inventamos capacidade. Para score >70, o pedido continua sujeito à exposição/capacidade.
    if capacidade_limite > 0:
        financial_available = capacidade_disponivel
    else:
        # Sem capacidade financeira demonstrada, não inventar disponibilidade.
        financial_available = 0.0
    comparison = compare_request_to_policy(score, pedido, policy_ceiling, financial_available, gate)
    limite = policy_ceiling
    disponivel = max(0.0, policy_ceiling - aberto)
    # Exposição é um controle independente e nunca é contada duas vezes.
    if capacidade_limite > 0:
        disponivel = min(disponivel, capacidade_disponivel)
    valor_aprovado = comparison["approved"]
    decisao_financeira = comparison["status"]

    prazo, entrada, decisao = conditions(risco,disponivel,pedido)
    if decisao_financeira in ("NÃO APROVADO", "BLOQUEADO", "PENDENTE", "REVISÃO MANUAL"):
        decisao = decisao_financeira
        prazo, entrada = ("À vista", 0) if decisao_financeira == "NÃO APROVADO" else ("Revisão manual / pendente", 0)
    elif decisao_financeira == "APROVAÇÃO PARCIAL":
        decisao = "APROVAÇÃO PARCIAL"
        prazo, entrada = "Revisão/condição comercial", 0
    elif decisao_financeira == "APROVAÇÃO INTEGRAL":
        decisao = "APROVADO"


    st.divider()
    st.subheader("3A. Informações públicas encontradas")
    if public_ok:
        pp1,pp2,pp3,pp4=st.columns(4)
        pp1.metric("Fontes respondidas", public_sources_ok)
        pp2.metric("Campos públicos", len(public_fields))
        pp3.metric("Sócios encontrados", len(dossier.get("partners",[])))
        pp4.metric("Divergências", public_conflicts)
        if public_conflicts:
            st.warning("Há divergências cadastrais entre fontes públicas. Isso reduz a confiabilidade da análise e exige confirmação documental.")
        else:
            st.success("As fontes públicas consultadas não apresentaram divergências nos campos extraídos automaticamente.")
    else:
        st.info("Nenhuma pesquisa pública foi carregada nesta análise. Clique em 'Pesquisar dados públicos agora' para trazer os dados disponíveis.")

    st.divider()
    st.subheader("4. Central de decisão")
    # Decision card first: user should not have to interpret several lines to know what to do.
    if score is not None and score < 25:
        st.error(f"### NÃO VENDER\nScore {score:.1f} — abaixo de 25. A política bloqueia qualquer faturamento a prazo.")
    elif decisao_financeira == "APROVAÇÃO INTEGRAL":
        st.success(f"### APROVAR ATÉ {money(valor_aprovado)}\nO pedido solicitado está dentro do limite permitido pela política e pelos controles financeiros.")
    elif decisao_financeira == "APROVAÇÃO PARCIAL":
        st.warning(f"### APROVAÇÃO PARCIAL: {money(valor_aprovado)}\nO cliente solicitou {money(pedido)}. O excedente de {money(comparison['gap'])} não deve ser faturado sem nova análise.")
    elif decisao_financeira in ("PENDENTE", "REVISÃO MANUAL"):
        st.warning(f"### {decisao_financeira}\nO sistema não recomenda liberar crédito automaticamente. Limite de política calculado: {money(policy_ceiling)}.")
    elif decisao_financeira == "NÃO APROVADO":
        st.error(f"### NÃO APROVAR\nPedido solicitado: {money(pedido)}. Limite permitido nesta análise: {money(policy_ceiling)}.")
    else:
        st.info(f"### LIMITE SUGERIDO: {money(policy_ceiling)}\nInforme a solicitação do cliente para obter a decisão final.")

    r1,r2,r3,r4,r5 = st.columns(5)
    r1.metric("Score", "N/D" if score is None else f"{score:.1f}")
    r2.metric("Cobertura", f"{cobertura}%")
    r3.metric("Risco", risco)
    r4.metric("Teto por score", money(policy_ceiling))
    r5.metric("Valor solicitado", money(pedido))

    st.write(f"**Faixa da política:** {policy['band']} — {policy['action']}")
    st.write(f"**Comparação solicitação × política:** solicitado {money(pedido)} | permitido pela faixa {money(policy_ceiling)} | aprovado nesta análise {money(valor_aprovado)}")
    st.write(f"**Qualidade dos dados:** {quality}")
    dso = dso_estimate(real, faturamento)
    st.write(f"**DSO estimado:** {'N/D' if dso is None else f'{dso:.1f} dias'}")
    st.write(f"**Tendência de comportamento:** {risk_trend(real)}")
    st.write(f"**Controle de decisão:** {gate} — {gate_reason}")
    if inconsistencias:
        st.warning("**Inconsistências para revisão:** " + " | ".join(inconsistencias))
    if public_conflicts:
        st.warning(f"**Divergências públicas:** {public_conflicts} campo(s) possuem valores diferentes entre as fontes consultadas.")

    st.write(f"**Limite comercial por score:** {money(policy_ceiling)}")
    st.write(f"**Limite financeiro calculado:** {money(capacidade_limite)}")
    st.write(f"**Exposição atual:** {money(aberto)}")
    st.write(f"**Limite disponível antes do pedido:** {money(disponivel)}")
    st.write(f"**Pedido solicitado:** {money(pedido)}")
    st.write(f"**Valor financeiro aprovado:** {money(valor_aprovado)}")
    if capacidade_limite > 0 and motor["utilization_before"] is not None:
        st.write(f"**Utilização financeira antes do pedido:** {motor['utilization_before']:.1f}%")
        st.write(f"**Utilização financeira após o pedido:** {motor['utilization_after']:.1f}%")
    st.caption(f"Motor financeiro: {motor['reason']}")
    if criterios_sem_evidencia:
        st.warning(
            "**Penalização por ausência de informação:** "
            + "; ".join(f"{c}: 25%" for c in criterios_sem_evidencia)
            + ". Esses critérios contribuíram com somente 25% da pontuação máxima."
        )
    st.write(f"**Decisão financeira:** {decisao_financeira} — {comparison['reason']}")
    st.write(f"**Condição sugerida:** {prazo} | **Entrada:** {money(entrada)}")
    st.write(f"**Decisão final:** {decisao}")

    with st.expander("Simulação financeira do pedido", expanded=True):
        st.markdown("Simule **qualquer valor** ou uma nova quantidade de caixas. O sistema recalcula a decisão imediatamente e compara a solicitação com o score, o teto da política, a exposição e o limite financeiro disponível.")
        s1,s2 = st.columns(2)
        with s1:
            sim_valor = st.number_input("Valor a simular (R$)", min_value=0.0, value=float(pedido or 0), step=100.0, key="sim_valor_48")
        with s2:
            sim_caixas = st.number_input("Ou quantidade total de caixas", min_value=0, value=0, step=1, key="sim_caixas_48")
        if sim_caixas > 0:
            sim_valor = sim_caixas * 9 * ((preco_montanhas + preco_desertos + preco_canions) / 3.0) * (1-desconto_pedido/100)
        sim_cmp = compare_request_to_policy(score, sim_valor, policy_ceiling if score is not None and score <=70 else sim_valor, financial_available, gate)
        sim_pos = max(0.0, disponivel - sim_valor)
        a,b,c = st.columns(3)
        a.metric("Solicitação simulada", money(sim_valor))
        b.metric("Valor aprovado", money(sim_cmp["approved"]))
        c.metric("Saldo após simulação", money(sim_pos))
        st.write(f"**Resultado da simulação:** {sim_cmp['status']}")
        st.write(f"**Motivo:** {sim_cmp['reason']}")
        if sim_cmp["gap"] > 0:
            st.warning(f"**Excedente não aprovado:** {money(sim_cmp['gap'])}")


    with st.expander("Ver composição do score"):
        for k,v in itens.items():
            st.write(
                f"**{k}** — "
                + (f"25.0/100 (ausência de evidência; 25%)" if v is None else f"{v:.1f}/100")
                + f" — peso {WEIGHTS[k]}%"
            )

    justificativas = []
    if criterios_sem_evidencia:
        justificativas.append(
            "Critérios sem evidência receberam somente 25% da pontuação máxima: "
            + ", ".join(criterios_sem_evidencia) + "."
        )
    if faturamento <= 0: justificativas.append("Sem faturamento informado/comprovado, não foi calculado limite por capacidade.")
    if fonte == "Declaração do cliente": justificativas.append("Faturamento declarado recebeu confiança menor que documentação financeira.")
    if tem_restricoes and restricoes > 0: justificativas.append(f"Foram informadas {restricoes} restrição(ões)/protesto(s) confirmado(s).")
    if tem_historico and atrasos is not None and atrasos > 0: justificativas.append(f"Há {atrasos} atraso(s); maior atraso informado: {maior_atraso if maior_atraso is not None else 'N/D'} dia(s).")
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
        audit(con, "CRIAR", "analise", cur.lastrowid, "", {
            "score": score, "risco": risco, "limite": limite,
            "disponivel": disponivel, "decisao": decisao
        }, "Nova análise de crédito")
        st.success("Análise salva com sucesso.")

        if REPORTLAB_OK:
            pdf = APP_DIR / f"relatorio_{cnpj_clean or 'cliente'}.pdf"
            pdf_report([
                ("CNPJ",cnpj_clean),("Razão social",razao),("Segmento",segmento),
                ("Score","N/D" if score is None else score),("Cobertura",f"{cobertura}%"),
                ("Risco",risco),("Limite aprovado total",money(limite)),
                ("Exposição atual",money(aberto)),
                ("Limite disponível",money(disponivel)),
                ("Pedido solicitado",money(pedido)),
                ("Valor aprovado",money(valor_aprovado)),
                ("Decisão financeira",decisao_financeira),
                ("Prazo sugerido",prazo),("Entrada",money(entrada)),
                ("Decisão final",decisao)
            ],pdf)
            with open(pdf,"rb") as f:
                st.download_button("Baixar relatório PDF",f,file_name=pdf.name)

elif menu == "Histórico real":
    st.subheader("Histórico real de clientes")
    clients = pd.read_sql_query("SELECT id, cnpj, razao FROM clientes ORDER BY razao", con)
    if clients.empty:
        st.info("Nenhum cliente cadastrado ainda.")
    else:
        selected = st.selectbox(
            "Cliente",
            clients["id"].tolist(),
            format_func=lambda x: f"{clients.loc[clients.id==x,'razao'].iloc[0]} — {clients.loc[clients.id==x,'cnpj'].iloc[0]}"
        )
        df = client_movements(con, selected)
        st.dataframe(df, use_container_width=True)
        if not df.empty:
            real = behavior_from_real_history(df)
            a,b,c,d = st.columns(4)
            a.metric("Compras registradas", money(real["purchases"]))
            b.metric("Atrasos", "N/D" if real["late_count"] is None else real["late_count"])
            c.metric("Pontualidade", "N/D" if real["on_time"] is None else f"{real['on_time']:.1f}%")
            d.metric("Em aberto", money(real["open"]))

        st.markdown("### Registrar movimento")
        with st.form("mov"):
            data_m = st.date_input("Data")
            tipo = st.selectbox("Tipo", ["Venda","Pagamento","Compra","Ajuste"])
            valor = st.number_input("Valor (R$)", min_value=0.0, step=100.0)
            venc = st.date_input("Vencimento")
            pag = st.selectbox("Status", ["Em aberto","Pago"])
            data_pag = st.date_input("Data do pagamento", value=date.today()) if pag == "Pago" else None
            atraso = st.number_input("Dias de atraso", min_value=0, step=1)
            categoria = st.selectbox("Categoria", ["Venda/Receita","Pagamento","Ajuste","Outro"])
            documento = st.text_input("Nº do documento / referência")
            obs = st.text_input("Observação")
            save = st.form_submit_button("Registrar")
        if save:
            cur = con.cursor()
            cur.execute("""INSERT INTO movimentos(cliente_id,data,tipo,valor,vencimento,pagamento,dias_atraso,observacao,data_pagamento,documento,categoria)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (selected,str(data_m),tipo,valor,str(venc),pag,atraso,obs,
                         str(data_pag) if data_pag else None,documento,categoria))
            con.commit()
            audit(con, "CRIAR", "movimento", cur.lastrowid, "", {
                "cliente_id": selected, "tipo": tipo, "valor": valor,
                "pagamento": pag, "dias_atraso": atraso
            }, "Registro de histórico real")
            st.success("Movimento registrado.")

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
    st.subheader("Metodologia")
    st.markdown("""
**Regra central:** informação ausente não é excluída do cálculo. Quando um critério relevante não possui evidência suficiente, ele recebe **25% da pontuação máxima daquele critério**. Isso reduz o score e impede que a falta de informação favoreça o cliente.

**Pesos**
- Cadastro e estabilidade: 20%
- Capacidade financeira: 25%
- Histórico de pagamento: 30%
- Exposição: 15%
- Comportamento operacional: 10%

**Cobertura:** mostra quanto da política total possui evidência suficiente. Ela não aumenta o score; serve como indicador independente de qualidade e governança.

**Faturamento:** a fonte é identificada. Declaração do cliente não é tratada como equivalente a documento financeiro.

**Limite:** não é inventado quando não existe base de capacidade. Histórico interno pode servir como base limitada quando houver comportamento comprovado.

**Restrições:** somente entram no cálculo quando a consulta foi efetivamente confirmada e registrada.

**Decisão:** o sistema separa risco, limite aprovado total, exposição, limite disponível, valor solicitado, valor aprovado e decisão financeira. Parâmetros comerciais específicos ficam fora do motor de risco.

**Histórico real:** títulos de venda/compra são separados de movimentos de pagamento; somente títulos entram no cálculo de exposição e comportamento. O sistema calcula pontualidade, atraso médio, maior atraso, exposição vencida e tendência.

**Motor de limite 4.5:** calcula limite aprovado total, desconta somente a exposição em aberto e apresenta o limite disponível. O limite atual não é subtraído novamente, evitando dupla contagem. O histórico pode moderar o limite, mas não cria capacidade financeira inexistente.

**Auditoria:** decisões e registros importantes geram data, ação, entidade, valores e motivo. O banco local é a fonte operacional e pode ser submetido a backup periódico.

**Gates de segurança:** CNPJ não validado, restrição ativa, atraso >90 dias, baixa cobertura ou inconsistências podem impedir aprovação automática ou exigir revisão humana.

**Qualidade dos dados:** cada análise recebe uma classificação de qualidade com base na cobertura e na quantidade de fontes verificadas.

**Ausência de informação:** cada critério sem evidência suficiente recebe 25% da pontuação máxima. A ausência nunca vira pontuação positiva nem é removida do denominador.

**Política comercial por score:** abaixo de 25 = não vender; 25 a <35 = até R$ 1.500; 35 a <45 = até R$ 5.000; 45 a <55 = até R$ 10.000; 55 a 70 = até R$ 20.000; acima de 70 = valor solicitado pelo cliente, sempre sujeito aos controles de exposição/capacidade e aos gates de segurança.

**Solicitação do cliente:** o valor pedido é um dado independente e é comparado explicitamente com o teto da política. A quantidade de caixas também pode ser informada; cada caixa representa 9 garrafas e o valor é calculado antes da decisão.

**Decisão:** a Central de decisão apresenta primeiro uma instrução operacional clara: NÃO VENDER, NÃO APROVAR, APROVAÇÃO PARCIAL, APROVAR ATÉ X ou revisão manual. O score não é tratado como aprovação automática quando os gates de segurança não são atendidos.

**Princípio de explicabilidade:** toda decisão mostra score, cobertura, qualidade, limite, exposição, utilização, inconsistências, dados usados, critérios penalizados e motivo da decisão.

""")
