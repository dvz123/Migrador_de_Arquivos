import json
import os
import streamlit as st
from dotenv import load_dotenv

from migrador import (
    autenticar_azure_blob,
    autenticar_google_drive,
    listar_dados_destino,
    listar_dados_origem,
    migrar_arquivos,
    normalizar_nome_container,
)

load_dotenv()

st.set_page_config(
    page_title="Migração Google Drive -> Azure Blob",
    page_icon="☁️",
    layout="wide",
)

st.title("☁️ Migração de Arquivos: Google Drive → Azure Blob Storage")
st.markdown(
    "Transfira arquivos da pasta **`Atividade_P2`** do Google Drive diretamente para o contêiner no Azure."
)

# --- Barra Lateral: Configurações e Autenticação ---
with st.sidebar:
    st.header("1. Autenticação Google Drive")
    arquivo_credenciais = st.file_uploader(
        "JSON da Service Account (Google)", type=["json"]
    )

    st.header("2. Autenticação Azure Blob")
    metodo_azure = st.radio(
        "Método de Autenticação", ["Connection String", "Blob SAS URL"]
    )

    conn_str_default = os.getenv("AZURE_CONNECTION_STRING", "")
    sas_url_default = os.getenv("AZURE_SAS_URL", "")

    if metodo_azure == "Connection String":
        azure_conn_str = st.text_input(
            "Connection String", value=conn_str_default, type="password"
        )
        azure_sas_url = None
    else:
        azure_sas_url = st.text_input(
            "Blob SAS URL", value=sas_url_default, type="password"
        )
        azure_conn_str = None

    st.header("3. Origem e Destino")
    pasta_origem = st.text_input("Pasta no Google Drive", value="Atividade_P2")
    container_input = st.text_input(
        "Nome do Contêiner (Aluno_xxx)", value="aluno-seunome"
    )
    st.caption(
        f"Nome normalizado no Azure: **`{normalizar_nome_container(container_input)}`**"
    )


def obter_clientes():
    """Valida as entradas e retorna os clientes autenticados."""
    if arquivo_credenciais is not None:
        sa_info = json.load(arquivo_credenciais)
        arquivo_credenciais.seek(0)
    elif os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        sa_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
    else:
        st.error("Envie o arquivo JSON da Service Account do Google na barra lateral.")
        st.stop()

    drive_service = autenticar_google_drive(sa_info)
    blob_client = autenticar_azure_blob(
        connection_string=azure_conn_str, sas_url=azure_sas_url
    )
    return drive_service, blob_client


# --- Área Principal: Ações ---
col_btn1, col_btn2, col_btn3 = st.columns(3)

with col_btn1:
    btn_listar_origem = st.button("📂 Listar Arquivos (Origem)", use_container_width=True)

with col_btn2:
    btn_migrar = st.button(
        "🚀 Iniciar Migração", type="primary", use_container_width=True
    )

with col_btn3:
    btn_listar_destino = st.button("🪣 Listar Blobs (Destino)", use_container_width=True)

st.divider()

col_origem, col_destino = st.columns(2)

# Ação 1: Listar Origem
if btn_listar_origem:
    try:
        drive_svc, _ = obter_clientes()
        arquivos = listar_dados_origem(drive_svc, pasta_origem)
        with col_origem:
            st.subheader(f"Origem: `{pasta_origem}` ({len(arquivos)})")
            if arquivos:
                st.dataframe(arquivos, use_container_width=True)
            else:
                st.info("Nenhum arquivo encontrado na pasta de origem.")
    except Exception as e:
        st.error(f"Erro ao listar origem: {e}")

# Ação 2: Listar Destino
if btn_listar_destino:
    try:
        _, blob_cli = obter_clientes()
        blobs = listar_dados_destino(blob_cli, container_input)
        with col_destino:
            st.subheader(
                f"Destino: `{normalizar_nome_container(container_input)}` ({len(blobs)})"
            )
            if blobs:
                st.dataframe(blobs, use_container_width=True)
            else:
                st.info("Contêiner vazio ou ainda não criado.")
    except Exception as e:
        st.error(f"Erro ao listar destino: {e}")

# Ação 3: Executar Migração Completa
if btn_migrar:
    try:
        drive_svc, blob_cli = obter_clientes()
        with st.spinner("Migrando arquivos e registrando logs no console..."):
            resultados = migrar_arquivos(
                drive_svc, blob_cli, pasta_origem, container_input
            )

        st.subheader("📋 Relatório da Transferência")
        st.dataframe(resultados, use_container_width=True)

        sucessos = sum(1 for r in resultados if r["status"] == "Sucesso")
        erros = sum(1 for r in resultados if r["status"] == "Erro")
        st.success(f"Concluído: {sucessos} sucesso(s) | {erros} erro(s). Confira também o terminal/console.")

        # Atualiza automaticamente as listas de origem e destino
        with col_origem:
            st.subheader(f"Origem: `{pasta_origem}`")
            st.dataframe(listar_dados_origem(drive_svc, pasta_origem), use_container_width=True)
        with col_destino:
            st.subheader(f"Destino: `{normalizar_nome_container(container_input)}`")
            st.dataframe(listar_dados_destino(blob_cli, container_input), use_container_width=True)

    except Exception as e:
        st.error(f"Erro durante a migração: {e}")