import io
import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

APP_TITLE = "Análise de Crédito Empresarial 6.0"
DB_PATH = "credito_empresarial_6.db"
ASSETS = Path("assets")
TIMEOUT = 15

st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")

# ----------------------------- helpers -----------------------------
def clean_cnpj(v):
    return re.sub(r"\D", "", str(v or ""))

def fmt_cnpj(v):
    n = clean_cnpj(v)
    return f"{n[:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:]}" if len(n)==14 else str(v or "")

def valid_cnpj(cnpj):
    n = clean_cnpj(cnpj)
    if len(n) != 14 or len(set(n)) == 1:
        return False
    a = [int(x) for x in n]
    w1 = [5,4,3,2,9,8,7,6,5,4,3,2]
    w2 = [6,5,4,3,2,9,8,7,6,5,4,3,2]
    d1 = sum(x*w for x,w in zip(a[:12],w1)) % 11
    d1 = 0 if d1 < 2 else 11-d1
    d2 = sum(x*w for x,w in zip(a[:12]+[d1],w2)) % 11
    d2 = 0 if d2 < 2 else 11-d2
    return d1 == a[12] and d2 == a[13]

def money(v):
    return f"R$ {float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def safe_float(v):
    try: return float(v)
    except: return 0.0

def parse_date(v):
    if not v: return None
    try: return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except: return None

def age_years(v):
    d = parse_date(v)
    return None if not d else max(0, (date.today()-d).days/365.25)

def nonempty(v):
    return v is not None and str(v).strip() not in ("", "None", "nan", "NaN")

# ----------------------------- database -----------------------------
def conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, cnpj TEXT, razao TEXT, score REAL, risco TEXT,
        confianca REAL, cobertura REAL, limite REAL, prazo TEXT, entrada REAL,
        decisao TEXT, justificativa TEXT, data_json TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS clients (
        cnpj TEXT PRIMARY KEY, razao TEXT, fantasia TEXT, situacao TEXT,
        abertura TEXT, ultima_consulta TEXT, data_json TEXT
    )""")
    c.commit(); c.close()

init_db()

def save_client(d):
    c=conn()
    c.execute("""INSERT INTO clients VALUES (?,?,?,?,?,?,?)
                 ON CONFLICT(cnpj) DO UPDATE SET razao=excluded.razao,
                 fantasia=excluded.fantasia,situacao=excluded.situacao,
                 abertura=excluded.abertura,ultima_consulta=excluded.ultima_consulta,
                 data_json=excluded.data_json""",
              (d.get("cnpj"),d.get("razao"),d.get("fantasia"),d.get("situacao"),
               d.get("abertura"),datetime.now().isoformat(timespec="seconds"),
               json.dumps(d,ensure_ascii=False)))
    c.commit(); c.close()

def save_analysis(d):
    c=conn()
    c.execute("""INSERT INTO analyses
        (created_at,cnpj,razao,score,risco,confianca,cobertura,limite,prazo,entrada,decisao,justificativa,data_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now().isoformat(timespec="seconds"),d["cnpj"],d.get("razao",""),
         d["score"],d["risco"],d["confianca"],d["cobertura"],d["limite"],
         d["prazo"],d["entrada"],d["decisao"],d["justificativa"],
         json.dumps(d,ensure_ascii=False)))
    c.commit(); c.close()

# ----------------------------- CNPJ public lookup -----------------------------
def cnpj_brasilapi(cnpj):
    r=requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{clean_cnpj(cnpj)}",timeout=TIMEOUT)
    r.raise_for_status()
    return r.json(), "BrasilAPI"

def cnpj_ws(cnpj):
    r=requests.get(f"https://publica.cnpj.ws/cnpj/{clean_cnpj(cnpj)}",timeout=TIMEOUT)
    r.raise_for_status()
    return r.json(), "CNPJ.ws"

def normalize(raw, source):
    if source=="BrasilAPI":
        return {
            "cnpj":clean_cnpj(raw.get("cnpj")),
            "razao":raw.get("razao_social",""),
            "fantasia":raw.get("nome_fantasia",""),
            "situacao":raw.get("descricao_situacao_cadastral") or raw.get("situacao_cadastral"),
            "abertura":raw.get("data_inicio_atividade"),
            "porte":raw.get("porte"),
            "capital":safe_float(raw.get("capital_social")),
            "cidade":raw.get("municipio",""), "uf":raw.get("uf",""),
            "cnae":raw.get("cnae_fiscal_descricao",""),
            "natureza":raw.get("natureza_juridica",""),
            "source":source, "raw":raw
        }
    est=raw.get("estabelecimento") or {}
    return {
        "cnpj":clean_cnpj(est.get("cnpj") or raw.get("cnpj")),
        "razao":raw.get("razao_social",""),
        "fantasia":est.get("nome_fantasia",""),
        "situacao":est.get("situacao_cadastral"),
        "abertura":est.get("data_inicio_atividade"),
        "porte":(raw.get("porte") or {}).get("descricao") if isinstance(raw.get("porte"),dict) else raw.get("porte"),
        "capital":safe_float(raw.get("capital_social")),
        "cidade":(est.get("cidade") or {}).get("nome","") if isinstance(est.get("cidade"),dict) else "",
        "uf":(est.get("estado") or {}).get("sigla","") if isinstance(est.get("estado"),dict) else "",
        "cnae":((est.get("atividade_principal") or {}).get("descricao","") if isinstance(est.get("atividade_principal"),dict) else ""),
        "natureza":((raw.get("natureza_juridica") or {}).get("descricao","") if isinstance(raw.get("natureza_juridica"),dict) else ""),
        "source":source, "raw":raw
    }

def consult_cnpj(cnpj):
    errors=[]
    for fn in (cnpj_brasilapi, cnpj_ws):
        try:
            raw,src=fn(cnpj); return normalize(raw,src), errors
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError("Nenhuma fonte pública de CNPJ respondeu. " + " | ".join(errors))

# ----------------------------- scoring -----------------------------
# PRINCIPLE: missing data is excluded from both numerator and denominator.
# It never earns points and never loses points.
WEIGHTS = {
    "situação cadastral": 20,
    "idade da empresa": 10,
    "negativações/bureau": 25,
    "capacidade financeira": 20,
    "comportamento de pagamento": 15,
    "exposição atual": 10,
}

def criterion(name, score, available, detail, weight):
    return {"name":name,"score":score,"available":available,"detail":detail,"weight":weight}

def score_analysis(d):
    criteria=[]
    sit=(d.get("situacao") or "").upper()
    if sit:
        s=100 if "ATIVA" in sit else 0
        criteria.append(criterion("situação cadastral",s,True,sit,WEIGHTS["situação cadastral"]))
    else:
        criteria.append(criterion("situação cadastral",None,False,"Não informado",WEIGHTS["situação cadastral"]))

    age=age_years(d.get("abertura"))
    if age is not None:
        s=min(100,age/10*100)
        criteria.append(criterion("idade da empresa",s,True,f"{age:.1f} anos",WEIGHTS["idade da empresa"]))
    else:
        criteria.append(criterion("idade da empresa",None,False,"Não informado",WEIGHTS["idade da empresa"]))

    neg=d.get("negativacoes")
    protest=d.get("protestos")
    if neg is not None or protest is not None:
        n=max(0,safe_float(neg)); p=max(0,safe_float(protest))
        s=max(0,100 - min(100,n*20+p*15))
        criteria.append(criterion("negativações/bureau",s,True,f"Negativações: {int(n)} | Protestos: {int(p)}",WEIGHTS["negativações/bureau"]))
    else:
        criteria.append(criterion("negativações/bureau",None,False,"Não consultado/informado",WEIGHTS["negativações/bureau"]))

    fat=d.get("faturamento")
    pedido=d.get("pedido")
    if fat is not None and safe_float(fat)>0:
        ratio=safe_float(pedido)/safe_float(fat) if safe_float(fat)>0 else 1
        s=max(0,min(100,(1-ratio)*100))
        criteria.append(criterion("capacidade financeira",s,True,f"Pedido = {ratio*100:.1f}% do faturamento mensal",WEIGHTS["capacidade financeira"]))
    else:
        criteria.append(criterion("capacidade financeira",None,False,"Faturamento não informado por fonte confiável",WEIGHTS["capacidade financeira"]))

    total=d.get("pagamentos_total")
    dia=d.get("pagamentos_dia")
    atraso=d.get("atraso_medio")
    if total is not None and safe_float(total)>0 and dia is not None:
        ontime=max(0,min(100,safe_float(dia)/safe_float(total)*100))
        penalty=min(50,max(0,safe_float(atraso or 0)))
        s=max(0,ontime-penalty)
        criteria.append(criterion("comportamento de pagamento",s,True,f"{ontime:.1f}% em dia | atraso médio {safe_float(atraso):.1f} dias",WEIGHTS["comportamento de pagamento"]))
    else:
        criteria.append(criterion("comportamento de pagamento",None,False,"Histórico interno não informado",WEIGHTS["comportamento de pagamento"]))

    exp=d.get("exposicao")
    if exp is not None and fat is not None and safe_float(fat)>0:
        ratio=safe_float(exp)/safe_float(fat)
        s=max(0,min(100,(1-ratio)*100))
        criteria.append(criterion("exposição atual",s,True,f"Exposição = {ratio*100:.1f}% do faturamento mensal",WEIGHTS["exposição atual"]))
    else:
        criteria.append(criterion("exposição atual",None,False,"Exposição ou faturamento não informado",WEIGHTS["exposição atual"]))

    avail=[c for c in criteria if c["available"]]
    weight_total=sum(c["weight"] for c in avail)
    score=sum(c["score"]*c["weight"] for c in avail)/weight_total if weight_total else None
    coverage=weight_total/sum(WEIGHTS.values())*100
    confidence=coverage

    # Hard-stop rules based only on known negative facts.
    hard_stop = (sit and "ATIVA" not in sit) or (neg is not None and safe_float(neg)>0 and protest is not None and safe_float(protest)>0)
    if hard_stop:
        risk="ALTO"
    elif score is None or coverage < 45:
        risk="DADOS INSUFICIENTES"
    elif score>=80:
        risk="BAIXO"
    elif score>=60:
        risk="MÉDIO"
    else:
        risk="ALTO"

    # Credit terms only when enough evidence exists.
    if risk=="DADOS INSUFICIENTES":
        limit=0; entry=max(0,safe_float(pedido)); term="À vista"
        decision="ANÁLISE INCONCLUSIVA — solicitar dados"
    else:
        fatv=safe_float(fat or 0)
        expv=safe_float(exp or 0)
        if risk=="BAIXO":
            limit=min(max(fatv*0.25,3000),30000) if fatv else 0
            term="30/60" if safe_float(pedido)<=limit*1.5 and limit>0 else "À vista + 30"
            decision="APROVAR" if safe_float(pedido)<=max(limit-expv,0) else "APROVAR COM CONDIÇÃO"
        elif risk=="MÉDIO":
            limit=min(max(fatv*0.15,1500),12000) if fatv else 0
            term="À vista + 30" if limit else "À vista"
            decision="APROVAR COM CONDIÇÃO" if safe_float(pedido)<=max(limit-expv,0) else "REDUZIR LIMITE / ENTRADA"
        else:
            limit=0; term="À vista"; decision="À vista"
        entry=max(0,safe_float(pedido)-max(0,limit-expv))

    used=[f'{c["name"]}: {c["score"]:.1f}/100' for c in avail]
    missing=[c["name"] for c in criteria if not c["available"]]
    justification="Critérios considerados: " + (", ".join(used) if used else "nenhum")
    if missing: justification += ". Dados ausentes foram DESCONSIDERADOS: " + ", ".join(missing) + "."
    if hard_stop: justification += " Há informação cadastral/negativa suficiente para bloqueio de crédito."
    if risk=="DADOS INSUFICIENTES": justification += " A cobertura de dados está abaixo do mínimo para uma decisão de crédito confiável."

    return {
        "score": round(score,1) if score is not None else None,
        "risco":risk,"confianca":round(confidence,1),"cobertura":round(coverage,1),
        "limite":round(limit,2),"entrada":round(entry,2),"prazo":term,
        "decisao":decision,"justificativa":justification,"criteria":criteria
    }

# ----------------------------- PDF -----------------------------
def pdf_report(data):
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=A4,rightMargin=36,leftMargin=36,topMargin=36,bottomMargin=36)
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CenterTitle", parent=styles["Title"], alignment=TA_CENTER, fontSize=16))
    story=[Paragraph("ANÁLISE DE CRÉDITO EMPRESARIAL",styles["CenterTitle"]),Spacer(1,12)]
    rows=[
        ["CNPJ",fmt_cnpj(data["cnpj"])],
        ["Razão social",data.get("razao","")],
        ["Score",str(data.get("score") if data.get("score") is not None else "N/D")],
        ["Risco",data["risco"]],
        ["Cobertura dos dados",f'{data["cobertura"]:.1f}%'],
        ["Confiança",f'{data["confianca"]:.1f}%'],
        ["Limite sugerido",money(data["limite"])],
        ["Condição",data["prazo"]],
        ["Entrada",money(data["entrada"])],
        ["Decisão",data["decisao"]],
    ]
    t=Table(rows,colWidths=[150,330])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.whitesmoke),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [t,Spacer(1,14),Paragraph("Justificativa",styles["Heading2"]),Paragraph(data["justificativa"],styles["BodyText"]),Spacer(1,12),Paragraph("Critérios",styles["Heading2"])]
    crows=[["Critério","Status","Nota","Peso","Detalhe"]]
    for c in data["criteria"]:
        crows.append([c["name"],"Considerado" if c["available"] else "Desconsiderado",f'{c["score"]:.1f}' if c["score"] is not None else "N/D",str(c["weight"]),c["detail"]])
    ct=Table(crows,colWidths=[105,80,45,45,205],repeatRows=1)
    ct.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.35,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#222222")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTSIZE",(0,0),(-1,-1),7)]))
    story.append(ct)
    story.append(Spacer(1,10))
    story.append(Paragraph("Este relatório é uma ferramenta de apoio à decisão. Informações de bureaus dependem de contrato, autorização e disponibilidade da fonte.",styles["Italic"]))
    doc.build(story)
    return buf.getvalue()

# ----------------------------- UI -----------------------------
st.title(APP_TITLE)
st.caption("Regra central da versão 6.0: dado ausente não é bom nem ruim — fica fora do cálculo.")

menu=st.sidebar.radio("Menu",["Nova análise","Histórico","Integrações","Política","Sobre"])

if menu=="Nova análise":
    st.subheader("1. Identificação")
    c1,c2=st.columns([4,1])
    with c1:
        cnpj=st.text_input("CNPJ",placeholder="00.000.000/0000-00")
    with c2:
        consultar=st.button("CONSULTAR CNPJ",use_container_width=True)
    if consultar:
        if not valid_cnpj(cnpj):
            st.error("CNPJ inválido. Confira os 14 dígitos.")
        else:
            try:
                d,errs=consult_cnpj(cnpj)
                st.session_state["empresa"]=d
                save_client(d)
                st.success(f"Empresa localizada via {d['source']}.")
            except Exception as e:
                st.error(str(e))

    emp=st.session_state.get("empresa",{})
    if emp:
        st.subheader("2. Dados cadastrais")
        a,b,c=st.columns(3)
        a.metric("Razão social",emp.get("razao") or "Não informado")
        b.metric("Situação",emp.get("situacao") or "Não informado")
        c.metric("Abertura",emp.get("abertura") or "Não informado")
        st.write(f"**Fantasia:** {emp.get('fantasia') or 'Não informado'}")
        st.write(f"**Porte:** {emp.get('porte') or 'Não informado'}  |  **Capital social:** {money(emp.get('capital',0)) if emp.get('capital') else 'Não informado'}")
        st.write(f"**Município/UF:** {emp.get('cidade') or 'Não informado'} / {emp.get('uf') or 'N/D'}")
        st.caption(f"Fonte cadastral: {emp.get('source')}")

    st.subheader("3. Dados financeiros e comportamentais")
    st.info("Preencha somente dados comprovados. Se deixar vazio, o critério será desconsiderado e não alterará o score.")
    r1,r2,r3=st.columns(3)
    fat=r1.number_input("Faturamento mensal comprovado (R$)",min_value=0.0,step=1000.0)
    pedido=r2.number_input("Valor do pedido (R$)",min_value=0.0,step=100.0)
    exposicao=r3.number_input("Exposição atual (R$)",min_value=0.0,step=100.0)
    r4,r5,r6=st.columns(3)
    pagamentos_total=r4.number_input("Total de pagamentos registrados",min_value=0,step=1)
    pagamentos_dia=r5.number_input("Pagamentos em dia",min_value=0,step=1)
    atraso_medio=r6.number_input("Atraso médio (dias)",min_value=0.0,step=1.0)

    st.subheader("4. Bureau / restrições")
    st.caption("A integração automática com bureau exige contrato/API e credenciais. Sem isso, o sistema não inventa dados.")
    b1,b2,b3=st.columns(3)
    neg=b1.number_input("Negativações",min_value=0,step=1)
    prot=b2.number_input("Protestos",min_value=0,step=1)
    ext_score=b3.number_input("Score externo (opcional)",min_value=0.0,max_value=1000.0,step=1.0)
    bureau_source=st.text_input("Fonte do bureau (ex.: Serasa / outro)",placeholder="Deixe vazio se não houver consulta")

    # Treat zeros as missing for bureau fields because zero is not distinguishable from 'not queried' in this simple UI.
    # Explicit checkbox allows zero to be confirmed.
    z1,z2=st.columns(2)
    neg_confirm=z1.checkbox("Confirmo que negativações = 0",value=False)
    prot_confirm=z2.checkbox("Confirmo que protestos = 0",value=False)
    neg_val=neg if neg_confirm or neg>0 else None
    prot_val=prot if prot_confirm or prot>0 else None

    if st.button("CALCULAR ANÁLISE",type="primary",use_container_width=True):
        if not emp:
            st.warning("Consulte primeiro o CNPJ.")
        else:
            data={**emp,
                  "faturamento":fat if fat>0 else None,
                  "pedido":pedido if pedido>0 else None,
                  "exposicao":exposicao if exposicao>0 else None,
                  "pagamentos_total":pagamentos_total if pagamentos_total>0 else None,
                  "pagamentos_dia":pagamentos_dia if pagamentos_total>0 else None,
                  "atraso_medio":atraso_medio if pagamentos_total>0 else None,
                  "negativacoes":neg_val,"protestos":prot_val,
                  "bureau_source":bureau_source or None,
                  "score_externo":ext_score if ext_score>0 else None}
            result=score_analysis(data)
            data.update(result)
            data["cnpj"]=emp["cnpj"]
            save_analysis(data)
            st.session_state["resultado"]=data

    result=st.session_state.get("resultado")
    if result:
        st.divider()
        st.subheader("5. Resultado")
        k1,k2,k3,k4,k5=st.columns(5)
        k1.metric("Score", "N/D" if result["score"] is None else f'{result["score"]:.1f}/100')
        k2.metric("Risco",result["risco"])
        k3.metric("Cobertura",f'{result["cobertura"]:.1f}%')
        k4.metric("Limite",money(result["limite"]))
        k5.metric("Condição",result["prazo"])
        st.write(f"**Entrada:** {money(result['entrada'])}")
        st.write(f"**Decisão:** {result['decisao']}")
        if result["risco"]=="DADOS INSUFICIENTES":
            st.warning(result["justificativa"])
        elif result["risco"]=="ALTO":
            st.error(result["justificativa"])
        else:
            st.info(result["justificativa"])

        st.subheader("Composição do score")
        rows=[]
        for c in result["criteria"]:
            rows.append({"Critério":c["name"],"Situação":"Considerado" if c["available"] else "DESCONSIDERADO","Nota":c["score"] if c["score"] is not None else None,"Peso":c["weight"],"Detalhe":c["detail"]})
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)

        pdf=pdf_report(result)
        st.download_button("Baixar relatório PDF",pdf,f"analise_{clean_cnpj(result['cnpj'])}.pdf","application/pdf")

elif menu=="Histórico":
    st.subheader("Histórico de análises")
    df=pd.read_sql_query("SELECT created_at,cnpj,razao,score,risco,confianca,cobertura,limite,prazo,decisao FROM analyses ORDER BY id DESC",conn())
    if df.empty: st.info("Nenhuma análise registrada.")
    else:
        st.dataframe(df,use_container_width=True,hide_index=True)
        st.download_button("Exportar CSV",df.to_csv(index=False).encode("utf-8-sig"),"historico_credito.csv","text/csv")

elif menu=="Integrações":
    st.subheader("Integrações")
    st.markdown("### CNPJ público")
    st.success("BrasilAPI + CNPJ.ws configurados como fontes cadastrais.")
    st.markdown("### Bureau de crédito")
    st.warning("Serasa/SPC e outros bureaus não são consultados por scraping ou credenciais inventadas. A integração automática exige contrato/API autorizado.")
    st.markdown("A Serasa Experian disponibiliza APIs para relatórios PJ; os produtos podem retornar, conforme contratação, negativações, protestos, cheques, score e outros dados. Configure a integração conforme o produto e as credenciais do seu contrato.")
    st.code("""# Exemplo de configuração futura
SERASA_CLIENT_ID=...
SERASA_CLIENT_SECRET=...
SERASA_TOKEN_URL=...
SERASA_API_BASE=...
SERASA_REPORT=...
""",language="bash")
    st.caption("Quando você tiver as credenciais e o layout do produto contratado, o adaptador pode ser conectado sem alterar o motor de score.")

elif menu=="Política":
    st.subheader("Política de decisão 6.0")
    st.markdown("""
**1. Dados faltantes não recebem nota.** O peso do critério é retirado do denominador.

**2. Score não é confiança.** A tela mostra separadamente score e cobertura/confiança.

**3. Decisão exige evidência.** Com cobertura baixa, o resultado é “DADOS INSUFICIENTES”, e não “bom”.

**4. Fatos negativos comprovados têm efeito.** Situação cadastral irregular, negativações e protestos reduzem o risco conforme as regras.

**5. Faturamento não é inventado pelo CNPJ.** Só entra no cálculo quando informado por fonte confiável.

**6. Prazo e entrada dependem da evidência.** Quanto menor a evidência, menor o crédito concedido.

**7. Toda decisão é auditável.** O relatório mostra quais critérios entraram e quais foram desconsiderados.
""")

elif menu=="Sobre":
    st.subheader("Versão 6.0")
    st.write("Motor de análise de crédito empresarial com dados faltantes neutros, cobertura de evidências, regras de bloqueio, condições de pagamento e relatório auditável.")
    st.caption("As informações externas dependem da disponibilidade e autorização das fontes. O sistema não substitui uma política de crédito formal ou análise humana.")

st.sidebar.divider()
st.sidebar.caption("6.0 • dados ausentes = neutros • decisões auditáveis")

