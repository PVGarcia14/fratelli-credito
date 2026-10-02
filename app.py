import io, re, sqlite3, requests
from datetime import date, datetime
import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

DB = "credito_empresarial.db"
CNPJ_APIS = [
    ("BrasilAPI / Minha Receita", "https://brasilapi.com.br/api/cnpj/v1/{}"),
    ("CNPJ.ws", "https://publica.cnpj.ws/cnpj/{}"),
    ("ReceitaWS", "https://www.receitaws.com.br/v1/cnpj/{}"),
]

SEGMENTOS = {
    "Comércio": {"fator": .10, "prazo": 28},
    "Distribuição": {"fator": .15, "prazo": 28},
    "Serviços": {"fator": .08, "prazo": 21},
    "Indústria": {"fator": .12, "prazo": 28},
    "Hotelaria": {"fator": .10, "prazo": 28},
    "Outro": {"fator": .08, "prazo": 14},
}
HIST = {"Excelente":25, "Bom":20, "Regular":12, "Ruim":3, "Sem histórico":8}


def cx(): return sqlite3.connect(DB)

def init():
    c = cx()
    c.execute("""CREATE TABLE IF NOT EXISTS clientes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, cnpj TEXT UNIQUE, razao TEXT, fantasia TEXT,
        abertura TEXT, situacao TEXT, segmento TEXT, capital REAL, faturamento REAL,
        socios INTEGER, pedido REAL, prazo_solicitado INTEGER, compras REAL,
        atraso_medio REAL, maior_atraso REAL, divida_atual REAL, limite_atual REAL,
        pagamentos_no_prazo REAL, protestos INTEGER, porte TEXT, natureza TEXT,
        cnae TEXT, endereco TEXT, municipio TEXT, uf TEXT, fonte_cnpj TEXT, criado TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS analises(
        id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER, score REAL, risco TEXT,
        limite REAL, prazo INTEGER, entrada REAL, decisao TEXT, justificativa TEXT, criado TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor REAL)""")
    defaults={"tempo":15,"historico":20,"capacidade":20,"comportamento":20,
              "relacionamento":10,"cadastro":10,"limite_baixo":.20,"limite_medio":.10}
    for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO config VALUES (?,?)",(k,v))
    c.commit(); c.close()

def cfg():
    c=cx(); d=dict(c.execute("SELECT chave,valor FROM config")); c.close(); return d

def only_digits(x): return re.sub(r"\D", "", x or "")

def normalize_cnpj(x): return re.sub(r"[^0-9A-Za-z]", "", (x or "")).upper()

def cnpj_ok(x):
    # Current Receita CNPJ may contain letters in the first 12 positions.
    x = normalize_cnpj(x)
    return len(x) == 14 and bool(re.fullmatch(r"[0-9A-Z]{14}", x))

def format_cnpj(x):
    x = normalize_cnpj(x)
    return f"{x[:2]}.{x[2:5]}.{x[5:8]}/{x[8:12]}-{x[12:]}" if len(x)==14 and x.isdigit() else x

def money(x): return f"R$ {x:,.2f}".replace(",","X").replace(".",",").replace("X",".")

def parse_date(v):
    if not v: return None
    try: return date.fromisoformat(str(v)[:10])
    except Exception: return None

def extract_capital(data):
    for k in ("capital_social", "capitalSocial"):
        if data.get(k) is not None:
            try: return float(data[k] or 0)
            except Exception: pass
    return 0.0

def extract_partners(data):
    qsa = data.get("qsa") or data.get("socios") or []
    return len(qsa) if isinstance(qsa, list) else 0

def _text(v):
    return "" if v is None else str(v).strip()

def _float(v):
    try:
        if v is None or v == "": return 0.0
        if isinstance(v, (int, float)): return float(v)
        return float(str(v).replace(".", "").replace(",", ".")) if "," in str(v) else float(v)
    except Exception:
        return 0.0

def _join_address(*parts):
    return ", ".join(_text(x) for x in parts if _text(x))

def _map_brasilapi(data, clean):
    abertura = data.get("data_inicio_atividade") or data.get("data_abertura") or data.get("data_abertura_empresa")
    endereco = _join_address(data.get("logradouro"), data.get("numero"), data.get("complemento"), data.get("bairro"), data.get("cep"))
    cnae = data.get("cnae_fiscal_descricao") or data.get("cnae_fiscal") or ""
    return {
        "cnpj": data.get("cnpj") or clean,
        "razao": data.get("razao_social") or data.get("razao_social_nome_empresarial") or "",
        "fantasia": data.get("nome_fantasia") or "",
        "abertura": abertura or "",
        "situacao": (data.get("descricao_situacao_cadastral") or data.get("situacao_cadastral") or "").upper(),
        "capital": _float(data.get("capital_social")),
        "socios": len(data.get("qsa") or []) if isinstance(data.get("qsa") or [], list) else 0,
        "porte": data.get("porte") or "",
        "natureza": data.get("natureza_juridica") or "",
        "cnae": str(cnae),
        "endereco": endereco,
        "municipio": data.get("municipio") or "",
        "uf": data.get("uf") or "",
        "fonte": "BrasilAPI / Minha Receita",
    }

def _map_cnpjws(data, clean):
    est = data.get("estabelecimento") or {}
    porte = data.get("porte") or {}
    natureza = data.get("natureza_juridica") or {}
    principal = est.get("atividade_principal") or {}
    cnae = _join_address(principal.get("id"), principal.get("descricao"))
    endereco = _join_address(
        est.get("tipo_logradouro"), est.get("logradouro"), est.get("numero"),
        est.get("complemento"), est.get("bairro"), est.get("cep")
    )
    return {
        "cnpj": est.get("cnpj") or clean,
        "razao": data.get("razao_social") or "",
        "fantasia": est.get("nome_fantasia") or "",
        "abertura": est.get("data_inicio_atividade") or "",
        "situacao": _text(est.get("situacao_cadastral")).upper(),
        "capital": _float(data.get("capital_social")),
        "socios": len(data.get("socios") or []) if isinstance(data.get("socios") or [], list) else 0,
        "porte": porte.get("descricao") if isinstance(porte, dict) else _text(porte),
        "natureza": natureza.get("descricao") if isinstance(natureza, dict) else _text(natureza),
        "cnae": cnae,
        "endereco": endereco,
        "municipio": (est.get("cidade") or {}).get("nome", "") if isinstance(est.get("cidade"), dict) else "",
        "uf": (est.get("estado") or {}).get("sigla", "") if isinstance(est.get("estado"), dict) else "",
        "fonte": "CNPJ.ws",
    }

def _map_receitaws(data, clean):
    atividade = data.get("atividade_principal") or []
    cnae = ""
    if isinstance(atividade, list) and atividade:
        cnae = _join_address(atividade[0].get("code"), atividade[0].get("text"))
    endereco = _join_address(data.get("logradouro"), data.get("numero"), data.get("complemento"), data.get("bairro"), data.get("cep"))
    qsa = data.get("qsa") or []
    return {
        "cnpj": data.get("cnpj") or clean,
        "razao": data.get("nome") or "",
        "fantasia": data.get("fantasia") or "",
        "abertura": data.get("abertura") or "",
        "situacao": _text(data.get("situacao")).upper(),
        "capital": _float(data.get("capital_social")),
        "socios": len(qsa) if isinstance(qsa, list) else 0,
        "porte": data.get("porte") or "",
        "natureza": data.get("natureza_juridica") or "",
        "cnae": cnae,
        "endereco": endereco,
        "municipio": data.get("municipio") or "",
        "uf": data.get("uf") or "",
        "fonte": "ReceitaWS",
    }

def lookup_cnpj(cnpj):
    clean = normalize_cnpj(cnpj)
    if not cnpj_ok(clean):
        raise ValueError("CNPJ inválido. Informe 14 caracteres (números ou, no novo padrão, letras/números).")

    erros = []
    for nome, url in CNPJ_APIS:
        # CNPJ.ws e ReceitaWS aceitam apenas o padrão numérico tradicional.
        if not clean.isdigit() and nome != "BrasilAPI / Minha Receita":
            continue
        try:
            r = requests.get(url.format(clean), timeout=15, headers={"User-Agent": "FratelliCredito/3.1"})
            if r.status_code == 200:
                data = r.json()
                if nome == "BrasilAPI / Minha Receita": result = _map_brasilapi(data, clean)
                elif nome == "CNPJ.ws": result = _map_cnpjws(data, clean)
                else: result = _map_receitaws(data, clean)
                if result.get("razao") or result.get("situacao"):
                    return result
                erros.append(f"{nome}: resposta sem dados empresariais")
            elif r.status_code == 404:
                erros.append(f"{nome}: CNPJ não localizado na base/cache")
            elif r.status_code == 429:
                erros.append(f"{nome}: limite de consultas atingido")
            else:
                erros.append(f"{nome}: HTTP {r.status_code}")
        except requests.RequestException as e:
            erros.append(f"{nome}: falha de conexão")
        except ValueError:
            erros.append(f"{nome}: resposta inválida")

    raise ValueError("Não foi possível obter os dados cadastrais nas fontes disponíveis. " + " | ".join(erros))

def calc(data):
    w=cfg(); abertura=parse_date(data.get("abertura")) or date.today()
    anos=max(0,(date.today()-abertura).days/365.25)
    tempo=min(anos/10,1)*w["tempo"]
    hist=HIST[data["historico"]]*(w["historico"]/25)
    cobertura=data["faturamento"]/max(data["pedido"],1)
    cap=w["capacidade"] if cobertura>=5 else w["capacidade"]*.75 if cobertura>=3 else w["capacidade"]*.5 if cobertura>=1.5 else w["capacidade"]*.25
    pont_pag=max(0,min(1,data["pagamentos"]/100))
    atraso=0 if data["atraso"]<=0 else .5 if data["atraso"]<=7 else .25 if data["atraso"]<=30 else 0
    comportamento=(pont_pag*.65+atraso*.35)*w["comportamento"]
    rel=min(data["compras"]/15000,1)*w["relacionamento"]
    cadastro=max(0,w["cadastro"]-data["protestos"]*3)
    score=round(max(0,min(100,tempo+hist+cap+comportamento+rel+cadastro)),1)
    if score>=80:
        risco="BAIXO"; base=data["faturamento"]*SEGMENTOS[data["segmento"]]["fator"]; limite=min(max(base,3000),20000)
    elif score>=60:
        risco="MÉDIO"; limite=min(max(data["faturamento"]*w["limite_medio"],1500),8000)
    else:
        risco="ALTO"; limite=0
    limite=max(0,limite-data["divida"])
    utilizacao=(data["divida"]/max(data["limite_atual"],1))*100 if data["limite_atual"]>0 else 0
    if data["protestos"]>0: risco="ALTO" if score<80 else "MÉDIO"
    if data["situacao"] not in ("ATIVA", "ATIVO", "ACTIVE"): risco="ALTO"; limite=0
    if data["maior_atraso"]>60: risco="ALTO"; limite=0
    pedido=data["pedido"]
    if risco=="BAIXO" and pedido<=limite: decisao="APROVAR"
    elif risco in ("BAIXO","MÉDIO") and pedido<=limite*1.25 and pedido>0: decisao="APROVAR COM CONDIÇÃO"
    elif risco=="MÉDIO" and limite>0: decisao="REDUZIR LIMITE / ENTRADA"
    else: decisao="VENDA À VISTA"
    entrada=0 if decisao=="APROVAR" else max(0,pedido-limite) if decisao!="VENDA À VISTA" else pedido
    prazo=SEGMENTOS[data["segmento"]]["prazo"] if risco=="BAIXO" else 21 if risco=="MÉDIO" else 0
    motivos=[]
    if anos<1: motivos.append("empresa com menos de 1 ano")
    if data["situacao"] not in ("ATIVA","ATIVO","ACTIVE"): motivos.append("situação cadastral diferente de ativa")
    if data["protestos"]>0: motivos.append("existem ocorrências/protestos informados")
    if data["maior_atraso"]>60: motivos.append("maior atraso superior a 60 dias")
    if data["divida"]>0: motivos.append("há exposição financeira atual")
    if pedido>limite and pedido>0: motivos.append("pedido superior ao limite disponível")
    if not motivos: motivos.append("indicadores dentro da política interna")
    return score,risco,limite,prazo,entrada,decisao,"; ".join(motivos),utilizacao

def pdf(row):
    b=io.BytesIO(); doc=SimpleDocTemplate(b,pagesize=A4,leftMargin=36,rightMargin=36,topMargin=36,bottomMargin=36)
    s=getSampleStyleSheet(); s.add(ParagraphStyle(name="S",parent=s["BodyText"],fontSize=9,leading=12))
    story=[Paragraph("RELATÓRIO DE ANÁLISE DE CRÉDITO",s["Title"]),Paragraph(f"Emitido em {datetime.now():%d/%m/%Y %H:%M}",s["S"]),Spacer(1,16)]
    d=[["Empresa",row["razao"]],["CNPJ",format_cnpj(row["cnpj"])],["Segmento",row["segmento"]],["Situação cadastral",row["situacao"]],["Score",f'{row["score"]}/100'],["Risco",row["risco"]],["Decisão",row["decisao"]],["Limite disponível",money(row["limite"])],["Prazo recomendado",f'{int(row["prazo"])} dias'],["Entrada sugerida",money(row["entrada"])],["Pedido",money(row["pedido"])],["Justificativa",row["justificativa"]]]
    t=Table(d,colWidths=[145,360]); t.setStyle(TableStyle([("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E9E9E9")),("GRID",(0,0),(-1,-1),.4,colors.grey),("VALIGN",(0,0),(-1,-1),"TOP"),("FONTSIZE",(0,0),(-1,-1),9),("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story += [t,Spacer(1,15),Paragraph("Este relatório é uma ferramenta interna de apoio à decisão. Dados cadastrais podem ser obtidos de fontes externas; dados financeiros e comportamentais devem vir de fontes autorizadas ou ser informados pelo analista.",s)]
    doc.build(story); b.seek(0); return b

init()
st.set_page_config(page_title="Sistema de Análise de Crédito", page_icon="📊", layout="wide")
st.markdown("<style>.block-container{max-width:1250px;padding-top:1.5rem}.stMetric{border:1px solid #ddd;border-radius:10px;padding:8px}</style>",unsafe_allow_html=True)
# Optional custom logo: place logo.png beside app.py.
import os
logo_path="logo.png"
if os.path.exists(logo_path): st.sidebar.image(logo_path,use_container_width=True)
else: st.sidebar.title("📊 CRÉDITO EMPRESARIAL")
st.sidebar.caption("Crédito • Risco • Limites • Relatórios")
menu=st.sidebar.radio("Menu",["Dashboard","Nova análise","Clientes","Política","Integração CNPJ"])

if menu=="Dashboard":
    st.title("Dashboard de crédito")
    c=cx(); cl=pd.read_sql_query("SELECT * FROM clientes",c); an=pd.read_sql_query("SELECT * FROM analises",c); c.close()
    a,b,c,d=st.columns(4); a.metric("Empresas",len(cl)); b.metric("Análises",len(an)); c.metric("Limite aprovado",money(an.limite.sum()) if len(an) else "R$ 0,00"); d.metric("Pedidos analisados",money(cl.pedido.sum()) if len(cl) else "R$ 0,00")
    if len(an):
        st.subheader("Risco da carteira"); st.bar_chart(an.risco.value_counts()); st.subheader("Últimas decisões"); st.dataframe(an.sort_values("id",ascending=False).head(15),use_container_width=True,hide_index=True)
    else: st.info("Nenhuma análise realizada ainda.")

elif menu=="Nova análise":
    st.title("Análise completa de crédito")
    st.caption("Digite apenas o CNPJ para preencher automaticamente os dados cadastrais disponíveis. Dados de faturamento, pagamentos, exposição e limite não são dados públicos garantidos pelo CNPJ e continuam dependendo de fonte autorizada ou informação interna.")
    if "cnpj_data" not in st.session_state: st.session_state.cnpj_data={}
    c1,c2=st.columns([4,1])
    with c1: cnpj_input=st.text_input("CNPJ",value=st.session_state.cnpj_data.get("cnpj",""),placeholder="Digite o CNPJ")
    with c2: consultar=st.button("CONSULTAR CNPJ",type="primary",use_container_width=True)
    if consultar:
        if not cnpj_ok(cnpj_input): st.error("Informe um CNPJ válido com 14 caracteres.")
        else:
            try:
                with st.spinner("Consultando dados cadastrais..."): st.session_state.cnpj_data=lookup_cnpj(cnpj_input)
                st.success("Dados cadastrais carregados automaticamente.")
            except Exception as e: st.error(f"Não foi possível consultar o CNPJ: {e}")
    d0=st.session_state.cnpj_data
    if d0:
        st.success(f"Empresa encontrada: {d0.get('razao','')} — {format_cnpj(d0.get('cnpj',''))}")
        st.caption(f"Fonte: {d0.get('fonte','')}")
    with st.form("a"):
        c1,c2,c3=st.columns(3)
        with c1:
            cnpj=st.text_input("CNPJ *",value=d0.get("cnpj",cnpj_input))
            razao=st.text_input("Razão social",value=d0.get("razao",""),disabled=True)
            fantasia=st.text_input("Nome fantasia",value=d0.get("fantasia",""),disabled=True)
            abertura_default=parse_date(d0.get("abertura")) or date.today()
            abertura=st.date_input("Data de abertura",abertura_default,disabled=bool(d0))
            situacao=st.text_input("Situação cadastral",value=d0.get("situacao",""),disabled=bool(d0))
            socios=st.number_input("Número de sócios",0,100,int(d0.get("socios",0)))
        with c2:
            segmento=st.selectbox("Segmento",list(SEGMENTOS))
            capital=st.number_input("Capital social (R$)",0.,value=float(d0.get("capital",0)),step=1000.,disabled=bool(d0 and d0.get("capital") is not None))
            faturamento=st.number_input("Faturamento mensal estimado (R$)",0.,step=1000.)
            pedido=st.number_input("Valor do pedido (R$)",0.,step=100.)
            prazo=st.number_input("Prazo solicitado (dias)",0,180,28)
            divida=st.number_input("Crédito atualmente utilizado (R$)",0.,step=100.)
        with c3:
            historico=st.selectbox("Histórico de pagamento",list(HIST))
            pagamentos=st.slider("% pagamentos no prazo",0,100,100)
            atraso=st.number_input("Atraso médio (dias)",0.,step=1.)
            maior_atraso=st.number_input("Maior atraso histórico (dias)",0.,step=1.)
            compras=st.number_input("Compras anteriores / relacionamento (R$)",0.,step=500.)
            limite_atual=st.number_input("Limite atualmente concedido (R$)",0.,step=100.)
            protestos=st.number_input("Protestos/ocorrências informados",0,100,0)
        st.subheader("Dados cadastrais recuperados")
        st.write({k:d0.get(k) for k in ["porte","natureza","cnae","endereco","municipio","uf"] if d0.get(k)})
        go=st.form_submit_button("GERAR ANÁLISE",type="primary",use_container_width=True)
    if go:
        if not cnpj_ok(cnpj): st.error("Informe um CNPJ válido.")
        elif not razao: st.error("Consulte o CNPJ antes de gerar a análise.")
        else:
            data={"abertura":abertura.isoformat(),"situacao":situacao,"segmento":segmento,"faturamento":faturamento,"pedido":pedido,"prazo":prazo,"historico":historico,"pagamentos":pagamentos,"atraso":atraso,"maior_atraso":maior_atraso,"compras":compras,"divida":divida,"limite_atual":limite_atual,"protestos":protestos}
            score,risco,limite,ps,entrada,dec,just,util=calc(data)
            st.divider(); a,b,c,d,e=st.columns(5); a.metric("Score",f"{score}/100"); b.metric("Risco",risco); c.metric("Limite",money(limite)); d.metric("Prazo",f"{ps} dias"); e.metric("Entrada",money(entrada))
            if dec=="APROVAR": st.success("DECISÃO: APROVAR")
            elif dec=="APROVAR COM CONDIÇÃO": st.warning("DECISÃO: APROVAR COM CONDIÇÃO")
            elif dec=="REDUZIR LIMITE / ENTRADA": st.warning("DECISÃO: REDUZIR LIMITE / ENTRADA")
            else: st.error("DECISÃO: VENDA À VISTA")
            st.info(f"Justificativa: {just}")
            c=cx();
            cur=c.execute("""INSERT INTO clientes(cnpj,razao,fantasia,abertura,situacao,segmento,capital,faturamento,socios,pedido,prazo_solicitado,compras,atraso_medio,maior_atraso,divida_atual,limite_atual,pagamentos_no_prazo,protestos,porte,natureza,cnae,endereco,municipio,uf,fonte_cnpj,criado) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cnpj) DO UPDATE SET razao=excluded.razao,fantasia=excluded.fantasia,abertura=excluded.abertura,situacao=excluded.situacao,segmento=excluded.segmento,capital=excluded.capital,faturamento=excluded.faturamento,socios=excluded.socios,pedido=excluded.pedido,prazo_solicitado=excluded.prazo_solicitado,compras=excluded.compras,atraso_medio=excluded.atraso_medio,maior_atraso=excluded.maior_atraso,divida_atual=excluded.divida_atual,limite_atual=excluded.limite_atual,pagamentos_no_prazo=excluded.pagamentos_no_prazo,protestos=excluded.protestos,porte=excluded.porte,natureza=excluded.natureza,cnae=excluded.cnae,endereco=excluded.endereco,municipio=excluded.municipio,uf=excluded.uf,fonte_cnpj=excluded.fonte_cnpj""",
                (normalize_cnpj(cnpj),razao,fantasia,abertura.isoformat(),situacao,segmento,capital,faturamento,socios,pedido,prazo,compras,atraso,maior_atraso,divida,limite_atual,pagamentos,protestos,d0.get("porte",""),d0.get("natureza",""),d0.get("cnae",""),d0.get("endereco",""),d0.get("municipio",""),d0.get("uf",""),d0.get("fonte",""),datetime.now().isoformat(timespec="seconds")))
            c.commit(); cid=c.execute("SELECT id FROM clientes WHERE cnpj=?",(normalize_cnpj(cnpj),)).fetchone()[0]
            cur=c.execute("INSERT INTO analises(cliente_id,score,risco,limite,prazo,entrada,decisao,justificativa,criado) VALUES(?,?,?,?,?,?,?,?,?)",(cid,score,risco,limite,ps,entrada,dec,just,datetime.now().isoformat(timespec="seconds"))); c.commit(); c.close()
            row={"razao":razao,"cnpj":cnormalize if False else cnpj,"segmento":segmento,"situacao":situacao,"score":score,"risco":risco,"decisao":dec,"limite":limite,"prazo":ps,"entrada":entrada,"pedido":pedido,"justificativa":just}
            st.download_button("Baixar relatório PDF",pdf(row).getvalue(),f"credito_{normalize_cnpj(cnpj)}.pdf","application/pdf")

elif menu=="Clientes":
    st.title("Carteira de clientes")
    c=cx(); df=pd.read_sql_query("SELECT * FROM clientes ORDER BY id DESC",c); c.close()
    if df.empty: st.info("Nenhum cliente cadastrado.")
    else:
        q=st.text_input("Pesquisar CNPJ ou empresa")
        if q: df=df[df.cnpj.astype(str).str.contains(q,case=False,na=False)|df.razao.astype(str).str.contains(q,case=False,na=False)]
        st.dataframe(df,use_container_width=True,hide_index=True)
        st.download_button("Exportar CSV",df.to_csv(index=False).encode("utf-8-sig"),"clientes.csv","text/csv")

elif menu=="Política":
    st.title("Política de crédito"); w=cfg()
    with st.form("p"):
        nv={}
        for k,l in [("tempo","Tempo de empresa"),("historico","Histórico"),("capacidade","Capacidade"),("comportamento","Comportamento"),("relacionamento","Relacionamento"),("cadastro","Cadastro")]: nv[k]=st.number_input(l,0.,100.,float(w[k]),step=1.)
        nv["limite_baixo"]=st.number_input("Fator limite — baixo risco",0.,1.,float(w["limite_baixo"]),step=.01); nv["limite_medio"]=st.number_input("Fator limite — médio risco",0.,1.,float(w["limite_medio"]),step=.01)
        save=st.form_submit_button("SALVAR")
    if save:
        c=cx();
        for k,v in nv.items(): c.execute("UPDATE config SET valor=? WHERE chave=?",(v,k))
        c.commit(); c.close(); st.success("Política atualizada.")

else:
    st.title("Integração automática de CNPJ")
    st.success("A consulta automática está habilitada usando a BrasilAPI / Minha Receita como fonte cadastral.")
    st.markdown("### O CNPJ pode preencher automaticamente")
    st.markdown("- Razão social e nome fantasia\n- Situação cadastral\n- Data de abertura\n- Capital social\n- Quadro societário, quando disponível\n- Porte e natureza jurídica\n- CNAE\n- Endereço, município e UF")
    st.warning("Faturamento, histórico de pagamentos, dívidas, protestos e limites de crédito não são garantidos pela consulta cadastral do CNPJ. Esses campos precisam de dados internos ou de fontes de crédito autorizadas.")
    st.markdown("Fonte técnica: BrasilAPI documenta o endpoint `/cnpj/v1/{cnpj}` e informa que a consulta retorna dados cadastrais, situação, sócios e atividades econômicas. A Receita Federal também mantém serviço oficial de consulta CNPJ e API para integração empresarial.")
