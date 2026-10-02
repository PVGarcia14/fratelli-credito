import io, json, re, sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

APP_TITLE = "Fratelli Crédito 7.0"
DB_PATH = "fratelli_credito.db"
LOGO_PATH = Path("assets/fratelli_logo.png")

st.set_page_config(page_title=APP_TITLE, page_icon="🍾", layout="wide")


# ----------------------------- utilidades -----------------------------
def clean_cnpj(v): return re.sub(r"\D", "", str(v or ""))

def fmt_cnpj(v):
    n = clean_cnpj(v)
    return f"{n[:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:]}" if len(n)==14 else str(v or "")

def valid_cnpj(cnpj):
    n=clean_cnpj(cnpj)
    if len(n)!=14 or n==n[0]*14: return False
    x=list(map(int,n))
    w1=[5,4,3,2,9,8,7,6,5,4,3,2]
    w2=[6,5,4,3,2,9,8,7,6,5,4,3,2]
    d1=sum(a*b for a,b in zip(x[:12],w1))%11
    d1=0 if d1<2 else 11-d1
    d2=sum(a*b for a,b in zip(x[:12]+[d1],w2))%11
    d2=0 if d2<2 else 11-d2
    return d1==x[12] and d2==x[13]

def fmoney(v):
    return f"R$ {float(v or 0):,.2f}".replace(",","X").replace(".",",").replace("X",".")

def fdate(v):
    if not v: return ""
    try: return datetime.strptime(str(v)[:10],"%Y-%m-%d").date()
    except: return None

def years_active(v):
    d=fdate(v)
    return round(max(0,(date.today()-d).days/365.25),1) if d else None


# ----------------------------- banco -----------------------------
def db(): return sqlite3.connect(DB_PATH)

def init_db():
    con=db(); c=con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS clients(
      cnpj TEXT PRIMARY KEY, razao TEXT, fantasia TEXT, situacao TEXT,
      abertura TEXT, porte TEXT, segmento TEXT, capital REAL, raw TEXT,
      updated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS analyses(
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, cnpj TEXT,
      razao TEXT, segmento TEXT, score REAL, cobertura REAL, confianca TEXT,
      status TEXT, risco TEXT, faturamento REAL, faturamento_fonte TEXT,
      pedido REAL, exposicao REAL, limite REAL, disponivel REAL, condicao TEXT,
      entrada REAL, decisao TEXT, justificativa TEXT, dados TEXT)""")
    con.commit(); con.close()

init_db()


# ----------------------------- CNPJ -----------------------------
def normalize(raw, source):
    if source=="brasilapi":
        return {
          "cnpj":clean_cnpj(raw.get("cnpj")),
          "razao":raw.get("razao_social",""), "fantasia":raw.get("nome_fantasia",""),
          "situacao":raw.get("descricao_situacao_cadastral",""),
          "abertura":raw.get("data_inicio_atividade",""),
          "porte":raw.get("porte",""), "capital":float(raw.get("capital_social") or 0),
          "cidade":raw.get("municipio",""), "uf":raw.get("uf",""),
          "cnae":raw.get("cnae_fiscal_descricao",""), "fonte":source, "raw":raw}
    est=raw.get("estabelecimento") or {}
    cap=raw.get("capital_social") or 0
    try: cap=float(str(cap).replace(".","").replace(",","."))
    except: cap=0
    return {
      "cnpj":clean_cnpj(est.get("cnpj") or raw.get("cnpj")),
      "razao":raw.get("razao_social",""), "fantasia":est.get("nome_fantasia",""),
      "situacao":est.get("situacao_cadastral",""),
      "abertura":est.get("data_inicio_atividade",""),
      "porte":(raw.get("porte") or {}).get("descricao","") if isinstance(raw.get("porte"),dict) else str(raw.get("porte","")),
      "capital":cap,
      "cidade":(est.get("cidade") or {}).get("nome","") if isinstance(est.get("cidade"),dict) else "",
      "uf":(est.get("estado") or {}).get("sigla","") if isinstance(est.get("estado"),dict) else "",
      "cnae":(est.get("atividade_principal") or {}).get("descricao","") if isinstance(est.get("atividade_principal"),dict) else "",
      "fonte":source, "raw":raw}

def query_cnpj(cnpj):
    n=clean_cnpj(cnpj); errors=[]
    for name,url in [
      ("BrasilAPI",f"https://brasilapi.com.br/api/cnpj/v1/{n}"),
      ("CNPJ.ws",f"https://publica.cnpj.ws/cnpj/{n}")]:
        try:
            r=requests.get(url,headers={"User-Agent":"FratelliCredito/7.0"},timeout=10)
            if r.status_code==200:
                return normalize(r.json(),"brasilapi" if name=="BrasilAPI" else "cnpjws"),errors
            errors.append(f"{name}: HTTP {r.status_code}")
        except Exception as e: errors.append(f"{name}: {type(e).__name__}")
    return None,errors


# ----------------------------- motor 7.0 -----------------------------
# Critérios e pesos. Ausência de dado = N/D e NÃO entra no denominador.
WEIGHTS = {
    "cadastro_estabilidade":20,
    "capacidade_financeira":25,
    "historico_pagamento":30,
    "exposicao":15,
    "comportamento_operacional":10
}

SEGMENT_FACTOR={"Bar":0.06,"Restaurante":0.07,"Distribuidor":0.10,
                "Supermercado":0.08,"Empório":0.07,"Hotel":0.07,"Outro":0.05}

def score_activity(anos):
    if anos is None: return None
    if anos>=10:return 100
    if anos>=5:return 90
    if anos>=3:return 80
    if anos>=2:return 70
    if anos>=1:return 55
    return 40

def score_capacity(fat,pedido,expo):
    if fat<=0:return None
    ratio=(pedido+expo)/fat
    if ratio<=.02:return 100
    if ratio<=.05:return 90
    if ratio<=.10:return 75
    if ratio<=.20:return 55
    if ratio<=.35:return 35
    return 10

def score_payments(total,ontime,avg_delay,max_delay):
    if total<=0:return None
    pct=max(0,min(1,ontime/total))
    s=pct*100
    s-=min(30,avg_delay*1.5)
    if max_delay>60:s-=35
    elif max_delay>30:s-=20
    elif max_delay>15:s-=10
    return max(0,min(100,s))

def score_exposure(fat,expo):
    if fat<=0:return None
    r=expo/fat
    if r<=.02:return 100
    if r<=.05:return 90
    if r<=.10:return 75
    if r<=.20:return 55
    if r<=.35:return 35
    return 10

def score_operations(occ,atrasos,maior):
    if occ==0 and atrasos==0 and maior==0:
        return None
    s=100
    s-=min(50,occ*15)
    if maior>60:s-=35
    elif maior>30:s-=20
    elif maior>15:s-=10
    return max(0,min(100,s))

def score_cadastro(company):
    situ=str(company.get("situacao","")).upper()
    anos=years_active(company.get("abertura"))
    vals=[]
    if situ:
        vals.append(100 if any(x in situ for x in ["ATIVA","REGULAR"]) else 20)
    a=score_activity(anos)
    if a is not None: vals.append(a)
    return sum(vals)/len(vals) if vals else None

def analyze(d):
    scores={
      "cadastro_estabilidade":score_cadastro(d["company"]),
      "capacidade_financeira":score_capacity(d["faturamento"],d["pedido"],d["exposicao"]),
      "historico_pagamento":score_payments(d["total_pagamentos"],d["pagamentos_dia"],d["atraso_medio"],d["maior_atraso"]),
      "exposicao":score_exposure(d["faturamento"],d["exposicao"]),
      "comportamento_operacional":score_operations(d["ocorrencias"],d["titulos_atrasados"],d["maior_atraso"])
    }
    weighted=0; used=0
    for k,w in WEIGHTS.items():
        if scores[k] is not None:
            weighted += scores[k]*w; used += w
    coverage=used/sum(WEIGHTS.values())*100
    normalized=weighted/used if used else None

    # Segurança: sem dados suficientes não classifica como bom/ruim.
    if coverage < 50:
        status="DADOS INSUFICIENTES"; risco="NÃO CLASSIFICADO"; score=None
    else:
        score=round(normalized,1)
        hard=[]
        situ=str(d["company"].get("situacao","")).upper()
        if situ and not any(x in situ for x in ["ATIVA","REGULAR"]): hard.append("situação cadastral não ativa/regular")
        if d["maior_atraso"]>90: hard.append("maior atraso superior a 90 dias")
        if hard:
            risco="ELEVADO"
        elif score>=80: risco="CONTROLADO"
        elif score>=60: risco="MODERADO"
        else: risco="ELEVADO"
        status="ANÁLISE CONCLUÍDA"

    # Limite: sem capacidade financeira comprovada, não usar faturamento estimado.
    if d["faturamento"]>0 and score is not None:
        factor=SEGMENT_FACTOR[d["segmento"]]
        base=d["faturamento"]*factor
        if risco=="CONTROLADO": limite=min(30000,max(3000,base))
        elif risco=="MODERADO": limite=min(12000,max(1500,base*.6))
        else: limite=0
    elif d["compras_12m"]>0 and d["total_pagamentos"]>0 and score is not None and risco=="CONTROLADO":
        limite=min(5000,max(1000,d["compras_12m"]*.05))
    else:
        limite=0
    disponivel=max(0,limite-d["exposicao"])

    if status!="ANÁLISE CONCLUÍDA":
        condicao="PENDENTE DE INFORMAÇÕES"
        decisao="NÃO CONCEDER CRÉDITO AUTOMÁTICO"
        entrada=d["pedido"]
    elif risco=="CONTROLADO" and d["pedido"]<=disponivel:
        condicao="30/60" if d["pedido"]<=disponivel*.5 else "À vista + 30"
        decisao="APROVAR"
        entrada=0
    elif risco=="MODERADO" and disponivel>0:
        condicao="Entrada + 30"; decisao="APROVAR COM ENTRADA"; entrada=max(0,d["pedido"]-disponivel)
    elif risco=="ELEVADO":
        condicao="À vista"; decisao="VENDA À VISTA"; entrada=d["pedido"]
    else:
        condicao="Entrada + 30" if disponivel>0 else "À vista"
        decisao="APROVAR COM ENTRADA" if disponivel>0 else "VENDA À VISTA"
        entrada=max(0,d["pedido"]-disponivel) if disponivel>0 else d["pedido"]

    missing=[]
    if d["faturamento"]<=0: missing.append("faturamento")
    if d["total_pagamentos"]<=0: missing.append("histórico de pagamentos")
    if d["compras_12m"]<=0: missing.append("histórico comercial")
    if not d["company"].get("situacao"): missing.append("situação cadastral")
    if not d["exposicao"] and d["faturamento"]<=0: pass

    reasons=[]
    if d["company"].get("situacao"): reasons.append(f"situação cadastral: {d['company']['situacao']}")
    if years_active(d["company"].get("abertura")) is not None: reasons.append(f"{years_active(d['company'].get('abertura'))} anos de atividade")
    if d["faturamento"]>0: reasons.append(f"pedido + exposição representam {(d['pedido']+d['exposicao'])/d['faturamento']*100:.1f}% do faturamento informado")
    if d["total_pagamentos"]>0: reasons.append(f"{d['pagamentos_dia']/d['total_pagamentos']*100:.1f}% dos pagamentos registrados em dia")
    if d["maior_atraso"]>0: reasons.append(f"maior atraso informado: {d['maior_atraso']:.0f} dias")
    if d["ocorrencias"]>0: reasons.append(f"{d['ocorrencias']} ocorrência(s) operacional(is)")
    if missing: reasons.append("informações ausentes: "+", ".join(missing))
    if status=="DADOS INSUFICIENTES":
        reasons.append("os critérios ausentes foram excluídos do cálculo; o sistema não os tratou como positivos nem negativos")
    justification="; ".join(reasons)+"."

    return {**d,"scores":scores,"score":score,"coverage":round(coverage,1),
            "risco":risco,"status":status,"limite":limite,"disponivel":disponivel,
            "condicao":condicao,"entrada":entrada,"decisao":decisao,
            "justificativa":justification}


# ----------------------------- persistência -----------------------------
def save_analysis(r):
    con=db()
    con.execute("""INSERT INTO analyses(created_at,cnpj,razao,segmento,score,cobertura,confianca,status,risco,
      faturamento,faturamento_fonte,pedido,exposicao,limite,disponivel,condicao,entrada,decisao,justificativa,dados)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
      datetime.now().isoformat(timespec="seconds"),r["company"]["cnpj"],r["company"]["razao"],r["segmento"],
      r["score"],r["coverage"],("ALTA" if r["coverage"]>=80 else "MÉDIA" if r["coverage"]>=50 else "BAIXA"),
      r["status"],r["risco"],r["faturamento"],r["faturamento_fonte"],r["pedido"],r["exposicao"],r["limite"],
      r["disponivel"],r["condicao"],r["entrada"],r["decisao"],r["justificativa"],json.dumps(r,default=str,ensure_ascii=False)))
    con.commit(); con.close()

def save_client(c,segmento):
    con=db()
    con.execute("""INSERT INTO clients(cnpj,razao,fantasia,situacao,abertura,porte,segmento,capital,raw,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cnpj) DO UPDATE SET razao=excluded.razao,fantasia=excluded.fantasia,
      situacao=excluded.situacao,abertura=excluded.abertura,porte=excluded.porte,segmento=excluded.segmento,
      capital=excluded.capital,raw=excluded.raw,updated_at=excluded.updated_at""",(
      c["cnpj"],c["razao"],c["fantasia"],c["situacao"],c["abertura"],c["porte"],segmento,c["capital"],
      json.dumps(c["raw"],ensure_ascii=False),datetime.now().isoformat(timespec="seconds")))
    con.commit();con.close()


# ----------------------------- PDF -----------------------------
def make_pdf(r):
    b=io.BytesIO(); c=canvas.Canvas(b,pagesize=A4); w,h=A4
    x=18*mm;y=h-20*mm
    c.setFont("Helvetica-Bold",18);c.drawString(x,y,"FRATELLI CRÉDITO 7.0");y-=9*mm
    c.setFont("Helvetica-Bold",12);c.drawString(x,y,"Relatório de análise empresarial");y-=8*mm
    lines=[
      f"CNPJ: {fmt_cnpj(r['company']['cnpj'])}",
      f"Empresa: {r['company']['razao']}",
      f"Situação: {r['company']['situacao']}",
      f"Status: {r['status']}",
      f"Score: {'N/D' if r['score'] is None else str(r['score'])+'/100'}",
      f"Cobertura dos dados: {r['coverage']:.0f}%",
      f"Risco: {r['risco']}",
      f"Faturamento: {fmoney(r['faturamento']) if r['faturamento']>0 else 'Não informado'}",
      f"Limite: {fmoney(r['limite'])}",
      f"Disponível: {fmoney(r['disponivel'])}",
      f"Pedido: {fmoney(r['pedido'])}",
      f"Condição: {r['condicao']}",
      f"Entrada: {fmoney(r['entrada'])}",
      f"Decisão: {r['decisao']}"
    ]
    c.setFont("Helvetica",9)
    for line in lines:
        c.drawString(x,y,line[:110]);y-=5.5*mm
    y-=3*mm;c.setFont("Helvetica-Bold",10);c.drawString(x,y,"Justificativa");y-=5*mm
    c.setFont("Helvetica",8.5)
    words=r["justificativa"].split();line=""
    for word in words:
        if len(line+" "+word)>105:
            c.drawString(x,y,line);y-=4.5*mm;line=word
        else: line=(line+" "+word).strip()
    if line:c.drawString(x,y,line)
    c.save();return b.getvalue()


# ----------------------------- interface -----------------------------
def brand():
    if LOGO_PATH.exists(): st.image(str(LOGO_PATH),width=190)
    else: st.markdown("## 🍾 FRATELLI")
    st.caption("Análise de crédito empresarial 7.0")

def new_analysis():
    st.title("Nova análise")
    cnpj=st.text_input("CNPJ",placeholder="00.000.000/0000-00")
    if st.button("🔎 Consultar CNPJ",type="primary"):
        if not valid_cnpj(cnpj): st.error("CNPJ inválido.");return
        with st.spinner("Consultando dados cadastrais..."):
            c,errs=query_cnpj(cnpj)
        if c:
            st.session_state["company"]=c
            save_client(c,"Outro")
            st.success(f"Empresa encontrada pela fonte {c['fonte']}.")
        else:
            st.error("Não foi possível localizar o CNPJ.")
            for e in errs: st.caption(e)
            return
    c=st.session_state.get("company")
    if not c:
        st.info("Informe um CNPJ para iniciar.");return

    st.subheader("Dados encontrados automaticamente")
    a,b,d=st.columns(3)
    a.metric("Razão social",c["razao"] or "Não informado")
    b.metric("Nome fantasia",c["fantasia"] or "Não informado")
    d.metric("Situação",c["situacao"] or "Não informado")
    a,b,d=st.columns(3)
    a.metric("Abertura",c["abertura"] or "Não informado")
    b.metric("Porte",c["porte"] or "Não informado")
    d.metric("Capital social",fmoney(c["capital"]))
    st.caption(f"CNAE: {c['cnae'] or 'Não informado'} | Local: {c['cidade'] or '—'} / {c['uf'] or '—'}")

    st.subheader("Dados para análise")
    st.info("Regra 7.0: informação ausente é N/D. Ela não recebe pontos positivos nem negativos e é retirada do denominador do score.")
    seg=st.selectbox("Segmento",list(SEGMENT_FACTOR))
    a,b,d=st.columns(3)
    fat=a.number_input("Faturamento mensal comprovado/informado (R$)",min_value=0.0,step=1000.0)
    pedido=b.number_input("Valor do pedido (R$)",min_value=0.0,step=500.0)
    expo=d.number_input("Exposição atual (R$)",min_value=0.0,step=500.0)
    fonte=st.selectbox("Fonte do faturamento",["Não informado","Documento financeiro","Declaração do cliente","Fonte financeira autorizada","Histórico interno"])
    a,b,d=st.columns(3)
    compras=a.number_input("Compras nos últimos 12 meses (R$)",min_value=0.0,step=1000.0)
    total=b.number_input("Pagamentos registrados",min_value=0,step=1)
    dia=d.number_input("Pagamentos em dia",min_value=0,step=1)
    a,b,d,e=st.columns(4)
    tit=a.number_input("Títulos atrasados",min_value=0,step=1)
    atraso=a.number_input("Valor em atraso (R$)",min_value=0.0,step=500.0)
    medio=b.number_input("Atraso médio (dias)",min_value=0.0,step=1.0)
    maior=e.number_input("Maior atraso (dias)",min_value=0.0,step=1.0)
    a,b=st.columns(2)
    ocorr=a.number_input("Ocorrências/devoluções",min_value=0,step=1)
    restr=b.number_input("Restrições confirmadas",min_value=0,step=1)

    if st.button("⚡ Executar análise 7.0",type="primary",use_container_width=True):
        r=analyze({"company":c,"segmento":seg,"faturamento":fat,"faturamento_fonte":fonte,
                   "pedido":pedido,"exposicao":expo,"compras_12m":compras,"total_pagamentos":total,
                   "pagamentos_dia":min(dia,total),"titulos_atrasados":tit,"valor_atraso":atraso,
                   "atraso_medio":medio,"maior_atraso":maior,"ocorrencias":ocorr,"restricoes":restr})
        st.session_state["result"]=r;save_analysis(r)

    r=st.session_state.get("result")
    if not r:return
    st.subheader("Resultado")
    a,b,d,e=st.columns(4)
    a.metric("Score", "N/D" if r["score"] is None else f"{r['score']:.1f}/100")
    b.metric("Cobertura",f"{r['coverage']:.0f}%")
    d.metric("Status",r["status"])
    e.metric("Risco",r["risco"])
    if r["status"]=="DADOS INSUFICIENTES":
        st.warning("⚪ DADOS INSUFICIENTES — o sistema não classificou a empresa como boa ou ruim.")
    elif r["risco"]=="CONTROLADO": st.success("🟢 RISCO CONTROLADO")
    elif r["risco"]=="MODERADO": st.warning("🟡 RISCO MODERADO")
    else: st.error("🔴 RISCO ELEVADO")

    a,b,d=st.columns(3)
    a.metric("Limite recomendado",fmoney(r["limite"]))
    b.metric("Crédito disponível",fmoney(r["disponivel"]))
    d.metric("Entrada",fmoney(r["entrada"]))
    st.info(f"**Condição:** {r['condicao']}  |  **Decisão:** {r['decisao']}")
    st.markdown("### Justificativa")
    st.write(r["justificativa"])

    labels={"cadastro_estabilidade":"Cadastro e estabilidade","capacidade_financeira":"Capacidade financeira",
            "historico_pagamento":"Histórico de pagamento","exposicao":"Exposição",
            "comportamento_operacional":"Comportamento operacional"}
    comp=[]
    for k,v in r["scores"].items():
        comp.append({"Critério":labels[k],"Peso":WEIGHTS[k],"Resultado":"N/D" if v is None else round(v,1),
                     "Participou do score": "Não" if v is None else "Sim"})
    st.markdown("### Auditoria do score")
    st.dataframe(pd.DataFrame(comp),use_container_width=True,hide_index=True)

    st.caption("Cobertura é a parcela dos pesos para a qual havia dados suficientes. Ela não é uma probabilidade de inadimplência.")
    st.download_button("📄 Baixar relatório PDF",make_pdf(r),f"analise_{clean_cnpj(c['cnpj'])}.pdf","application/pdf",use_container_width=True)

def history():
    st.title("Histórico")
    con=db();df=pd.read_sql_query("SELECT created_at,cnpj,razao,score,cobertura,status,risco,limite,condicao,decisao FROM analyses ORDER BY id DESC",con);con.close()
    if df.empty:st.info("Sem análises.");return
    df["cnpj"]=df["cnpj"].apply(fmt_cnpj)
    st.dataframe(df,use_container_width=True,hide_index=True)
    st.download_button("Exportar CSV",df.to_csv(index=False).encode("utf-8-sig"),"historico.csv","text/csv")

def policy():
    st.title("Metodologia 7.0")
    st.markdown("""
### Princípio central
**Ausência de informação não é evidência positiva nem negativa.**

Quando um critério não tem dados confiáveis, ele recebe **N/D**, é excluído do denominador e não altera o score.

### Pesos
- Cadastro e estabilidade: **20%**
- Capacidade financeira: **25%**
- Histórico de pagamento: **30%**
- Exposição: **15%**
- Comportamento operacional: **10%**

### Estados
- 🟢 **Risco controlado**: score ≥ 80, com cobertura mínima de 50%.
- 🟡 **Risco moderado**: score de 60 a 79,9, com cobertura mínima de 50%.
- 🔴 **Risco elevado**: score < 60 ou gatilho forte de risco.
- ⚪ **Dados insuficientes**: cobertura < 50%.

### Regra de segurança
Sem faturamento comprovado, o sistema **não estima receita para liberar crédito**. O limite fica conservador até existir evidência suficiente.

A metodologia é um apoio à decisão e deve ser validada pela política financeira da empresa antes de uso operacional.
""")

def clients():
    st.title("Clientes")
    con=db();df=pd.read_sql_query("SELECT cnpj,razao,fantasia,situacao,abertura,porte,segmento,capital,updated_at FROM clients ORDER BY updated_at DESC",con);con.close()
    if df.empty:st.info("Nenhum cliente.");return
    df["cnpj"]=df["cnpj"].apply(fmt_cnpj)
    st.dataframe(df,use_container_width=True,hide_index=True)

def app():
    brand()
    page=st.sidebar.radio("Menu",["Nova análise","Histórico","Clientes","Metodologia 7.0"])
    st.sidebar.caption("Dados ausentes = N/D; não interferem no score.")
    if page=="Nova análise":new_analysis()
    elif page=="Histórico":history()
    elif page=="Clientes":clients()
    else:policy()

app()
