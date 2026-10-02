
import sqlite3, io, re
from datetime import date, datetime
import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

DB="fratelli_credito.db"

SEGMENTOS={
 "Bar": {"fator":.08,"prazo":21,"risco_extra":0},
 "Restaurante":{"fator":.10,"prazo":28,"risco_extra":0},
 "Distribuidor":{"fator":.15,"prazo":28,"risco_extra":1},
 "Supermercado":{"fator":.12,"prazo":28,"risco_extra":0},
 "Empório":{"fator":.10,"prazo":28,"risco_extra":0},
 "Hotel":{"fator":.10,"prazo":28,"risco_extra":0},
 "Outro":{"fator":.08,"prazo":14,"risco_extra":0},
}
HIST={"Excelente":25,"Bom":20,"Regular":12,"Ruim":3,"Sem histórico":8}

def cx(): return sqlite3.connect(DB)
def init():
 c=cx()
 c.execute("""CREATE TABLE IF NOT EXISTS clientes(
 id INTEGER PRIMARY KEY AUTOINCREMENT, cnpj TEXT UNIQUE, razao TEXT, fantasia TEXT,
 abertura TEXT, situacao TEXT, segmento TEXT, capital REAL, faturamento REAL,
 socios INTEGER, pedido REAL, prazo_solicitado INTEGER, compras REAL,
 atraso_medio REAL, maior_atraso REAL, divida_atual REAL, limite_atual REAL,
 pagamentos_no_prazo REAL, protestos INTEGER, observacoes TEXT, criado TEXT)""")
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
def cnpj_ok(x): return len(re.sub(r"\D","",x))==14
def money(x): return f"R$ {x:,.2f}".replace(",","X").replace(".",",").replace("X",".")
def calc(data):
 w=cfg()
 abertura=date.fromisoformat(data["abertura"])
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

 # Exposição: pedido + dívida atual não deve ultrapassar limite recomendado.
 if score>=80:
  risco="BAIXO"; base=data["faturamento"]*SEGMENTOS[data["segmento"]]["fator"]
  limite=min(max(base,3000),20000)
 elif score>=60:
  risco="MÉDIO"; limite=min(max(data["faturamento"]*w["limite_medio"],1500),8000)
 else:
  risco="ALTO"; limite=0

 limite=max(0,limite-data["divida"])
 utilizacao=(data["divida"]/max(data["limite_atual"],1))*100 if data["limite_atual"]>0 else 0
 if data["protestos"]>0: risco="ALTO" if score<80 else "MÉDIO"
 if data["situacao"]!="ATIVA": risco="ALTO"; limite=0
 if data["maior_atraso"]>60: risco="ALTO"; limite=0

 pedido=data["pedido"]
 if risco=="BAIXO" and pedido<=limite:
  decisao="APROVAR"
 elif risco in ("BAIXO","MÉDIO") and pedido<=limite*1.25 and pedido>0:
  decisao="APROVAR COM CONDIÇÃO"
 elif risco=="MÉDIO" and limite>0:
  decisao="REDUZIR LIMITE / ENTRADA"
 else:
  decisao="VENDA À VISTA"

 if decisao=="APROVAR": entrada=0
 elif decisao=="APROVAR COM CONDIÇÃO": entrada=max(0,pedido-limite)
 elif decisao=="REDUZIR LIMITE / ENTRADA": entrada=max(0,pedido-limite)
 else: entrada=pedido

 prazo=SEGMENTOS[data["segmento"]]["prazo"] if risco=="BAIXO" else 21 if risco=="MÉDIO" else 0
 motivos=[]
 if anos<1: motivos.append("empresa com menos de 1 ano")
 if data["situacao"]!="ATIVA": motivos.append("situação cadastral diferente de ativa")
 if data["protestos"]>0: motivos.append("existem ocorrências/protestos informados")
 if data["maior_atraso"]>60: motivos.append("maior atraso superior a 60 dias")
 if data["divida"]>0: motivos.append("há exposição financeira atual")
 if data["pedido"]>limite and pedido>0: motivos.append("pedido superior ao limite disponível")
 if not motivos: motivos.append("indicadores dentro da política interna")
 return score,risco,limite,prazo,entrada,decisao,"; ".join(motivos),utilizacao

def pdf(row):
 b=io.BytesIO(); doc=SimpleDocTemplate(b,pagesize=A4,leftMargin=36,rightMargin=36,topMargin=36,bottomMargin=36)
 s=getSampleStyleSheet(); s.add(ParagraphStyle(name="S",parent=s["BodyText"],fontSize=9,leading=12))
 story=[Paragraph("FRATELLI — RELATÓRIO DE CRÉDITO 2.0",s["Title"]),
        Paragraph(f"Emitido em {datetime.now():%d/%m/%Y %H:%M}",s["S"]),Spacer(1,16)]
 d=[["Empresa",row["razao"]],["CNPJ",row["cnpj"]],["Segmento",row["segmento"]],
 ["Situação cadastral",row["situacao"]],["Score",f'{row["score"]}/100'],["Risco",row["risco"]],
 ["Decisão",row["decisao"]],["Limite disponível",money(row["limite"])],
 ["Prazo recomendado",f'{int(row["prazo"])} dias'],["Entrada sugerida",money(row["entrada"])],
 ["Pedido",money(row["pedido"])],["Justificativa",row["justificativa"]]]
 t=Table(d,colWidths=[145,360]); t.setStyle(TableStyle([
 ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E9E9E9")),("GRID",(0,0),(-1,-1),.4,colors.grey),
 ("VALIGN",(0,0),(-1,-1),"TOP"),("FONTSIZE",(0,0),(-1,-1),9),("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
 story += [t,Spacer(1,15),Paragraph("Este relatório é uma ferramenta interna de apoio à decisão e depende da qualidade dos dados informados e/ou obtidos de fontes autorizadas.",s)]
 doc.build(story); b.seek(0); return b

init()
st.set_page_config(page_title="Fratelli Crédito 2.0",page_icon="🥃",layout="wide")
st.markdown("""<style>.block-container{max-width:1250px;padding-top:1.5rem}.stMetric{border:1px solid #ddd;border-radius:10px;padding:8px}</style>""",unsafe_allow_html=True)
st.sidebar.title("🥃 FRATELLI")
st.sidebar.caption("Crédito • Risco • Vendas")
menu=st.sidebar.radio("Menu",["Dashboard","Nova análise","Clientes","Política","Integração CNPJ"])

if menu=="Dashboard":
 st.title("Dashboard de crédito")
 c=cx(); cl=pd.read_sql_query("SELECT * FROM clientes",c); an=pd.read_sql_query("SELECT * FROM analises",c); c.close()
 a,b,c,d=st.columns(4)
 a.metric("Empresas",len(cl)); b.metric("Análises",len(an)); c.metric("Limite aprovado",money(an.limite.sum()) if len(an) else "R$ 0,00"); d.metric("Pedidos analisados",money(cl.pedido.sum()) if len(cl) else "R$ 0,00")
 if len(an):
  st.subheader("Risco da carteira"); st.bar_chart(an.risco.value_counts())
  st.subheader("Últimas decisões"); st.dataframe(an.sort_values("id",ascending=False).head(15),use_container_width=True,hide_index=True)
 else: st.info("Nenhuma análise realizada ainda.")

elif menu=="Nova análise":
 st.title("Análise completa de crédito")
 with st.form("a"):
  c1,c2,c3=st.columns(3)
  with c1:
   cnpj=st.text_input("CNPJ *"); razao=st.text_input("Razão social *"); fantasia=st.text_input("Nome fantasia")
   abertura=st.date_input("Data de abertura",date.today()); situacao=st.selectbox("Situação cadastral",["ATIVA","INAPTA","BAIXADA","SUSPENSA"])
   socios=st.number_input("Número de sócios",0,100,1)
  with c2:
   segmento=st.selectbox("Segmento",list(SEGMENTOS)); capital=st.number_input("Capital social (R$)",0.,step=1000.)
   faturamento=st.number_input("Faturamento mensal estimado (R$)",0.,step=1000.)
   pedido=st.number_input("Valor do pedido (R$)",0.,step=100.)
   prazo=st.number_input("Prazo solicitado (dias)",0,180,28)
  with c3:
   historico=st.selectbox("Histórico de pagamento",list(HIST)); pagamentos=st.slider("% pagamentos no prazo",0,100,100)
   atraso=st.number_input("Atraso médio (dias)",0.,step=1.); maior_atraso=st.number_input("Maior atraso histórico (dias)",0.,step=1.)
   compras=st.number_input("Compras anteriores Fratelli (R$)",0.,step=500.)
   divida=st.number_input("Crédito atualmente utilizado (R$)",0.,step=100.)
   limite_atual=st.number_input("Limite atualmente concedido (R$)",0.,step=100.)
   protestos=st.number_input("Protestos/ocorrências informados",0,100,0)
  go=st.form_submit_button("GERAR ANÁLISE",type="primary",use_container_width=True)
 if go:
  if not cnpj_ok(cnpj): st.error("Informe um CNPJ com 14 dígitos.")
  elif not razao: st.error("Razão social é obrigatória.")
  else:
   data={"abertura":abertura.isoformat(),"situacao":situacao,"segmento":segmento,"faturamento":faturamento,"pedido":pedido,"prazo":prazo,"historico":historico,"pagamentos":pagamentos,"atraso":atraso,"maior_atraso":maior_atraso,"compras":compras,"divida":divida,"limite_atual":limite_atual,"protestos":protestos}
   score,risco,limite,ps,entrada,dec,just,util=calc(data)
   st.divider(); a,b,c,d,e=st.columns(5)
   a.metric("Score",f"{score}/100"); b.metric("Risco",risco); c.metric("Limite",money(limite)); d.metric("Prazo",f"{ps} dias"); e.metric("Entrada",money(entrada))
   if dec=="APROVAR": st.success("DECISÃO: APROVAR")
   elif dec=="APROVAR COM CONDIÇÃO": st.warning("DECISÃO: APROVAR COM CONDIÇÃO")
   elif dec=="REDUZIR LIMITE / ENTRADA": st.warning("DECISÃO: REDUZIR LIMITE / ENTRADA")
   else: st.error("DECISÃO: VENDA À VISTA")
   st.info(f"Justificativa: {just}")
   c=cx(); cur=c.execute("""INSERT OR IGNORE INTO clientes(cnpj,razao,fantasia,abertura,situacao,segmento,capital,faturamento,socios,pedido,prazo_solicitado,compras,atraso_medio,maior_atraso,divida_atual,limite_atual,pagamentos_no_prazo,protestos,criado)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(cnpj,razao,fantasia,abertura.isoformat(),situacao,segmento,capital,faturamento,socios,pedido,prazo,compras,atraso,maior_atraso,divida,limite_atual,pagamentos,protestos,datetime.now().isoformat(timespec="seconds")))
   c.commit(); cid=cur.lastrowid
   if cid==0: cid=c.execute("SELECT id FROM clientes WHERE cnpj=?",(cnpj,)).fetchone()[0]
   cur=c.execute("""INSERT INTO analises(cliente_id,score,risco,limite,prazo,entrada,decisao,justificativa,criado) VALUES(?,?,?,?,?,?,?,?,?)""",(cid,score,risco,limite,ps,entrada,dec,just,datetime.now().isoformat(timespec="seconds")))
   aid=cur.lastrowid; c.commit(); c.close()
   row={"razao":razao,"cnpj":cnpj,"segmento":segmento,"situacao":situacao,"score":score,"risco":risco,"decisao":dec,"limite":limite,"prazo":ps,"entrada":entrada,"pedido":pedido,"justificativa":just}
   st.download_button("Baixar relatório PDF",pdf(row).getvalue(),f"fratelli_credito_{cnpj}.pdf","application/pdf")

elif menu=="Clientes":
 st.title("Carteira de clientes")
 c=cx(); df=pd.read_sql_query("SELECT * FROM clientes ORDER BY id DESC",c); c.close()
 if df.empty: st.info("Nenhum cliente cadastrado.")
 else:
  q=st.text_input("Pesquisar CNPJ ou empresa")
  if q: df=df[df.cnpj.str.contains(q,case=False,na=False)|df.razao.str.contains(q,case=False,na=False)]
  st.dataframe(df,use_container_width=True,hide_index=True)
  st.download_button("Exportar CSV",df.to_csv(index=False).encode("utf-8-sig"),"fratelli_clientes.csv","text/csv")

elif menu=="Política":
 st.title("Política de crédito")
 w=cfg()
 with st.form("p"):
  nv={}
  for k,l in [("tempo","Tempo de empresa"),("historico","Histórico"),("capacidade","Capacidade"),("comportamento","Comportamento"),("relacionamento","Relacionamento Fratelli"),("cadastro","Cadastro")]:
   nv[k]=st.number_input(l,0.,100.,float(w[k]),step=1.)
  nv["limite_baixo"]=st.number_input("Fator limite — baixo risco",0.,1.,float(w["limite_baixo"]),step=.01)
  nv["limite_medio"]=st.number_input("Fator limite — médio risco",0.,1.,float(w["limite_medio"]),step=.01)
  save=st.form_submit_button("SALVAR")
 if save:
  c=cx()
  for k,v in nv.items(): c.execute("UPDATE config SET valor=? WHERE chave=?",(v,k))
  c.commit(); c.close(); st.success("Política atualizada.")

else:
 st.title("Integração automática de CNPJ")
 st.info("A versão 2.0 já possui a camada de dados separada do motor de crédito. Para consultas automáticas, conecte uma API de CNPJ autorizada.")
 st.markdown("""### Campos que a integração pode preencher automaticamente
- Razão social e nome fantasia
- Situação cadastral
- Data de abertura
- CNAE
- Endereço
- Capital social
- Quadro societário, quando fornecido pela fonte
- Porte/natureza jurídica
- Dados cadastrais disponíveis

**Importante:** o sistema não deve inventar dados ausentes. Campos não retornados pela fonte permanecem pendentes para análise.""")
 st.code("""# interface planejada
class CNPJProvider:
    def consultar(self, cnpj: str) -> dict:
        # conectar aqui uma API autorizada
        return {
            "razao": "...",
            "fantasia": "...",
            "situacao": "ATIVA",
            "abertura": "2020-01-01",
            "capital": 100000
        }
""",language="python")
