import io
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Configuração do logger para exibir status em tempo real no console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("MigradorDriveAzure")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Mapeamento para exportar arquivos nativos do Google (Docs, Sheets, Slides)
GOOGLE_MIME_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}


def normalizar_nome_container(nome: str) -> str:
    """Converte o nome do contêiner para o padrão válido do Azure (minúsculas e hífens)."""
    nome_limpo = re.sub(r"[^a-z0-9-]", "-", nome.strip().lower())
    nome_limpo = re.sub(r"-+", "-", nome_limpo).strip("-")
    return nome_limpo or "aluno-padrao"


def autenticar_google_drive(service_account_info: Dict):
    """Autentica na API do Google Drive usando credenciais de Service Account."""
    try:
        creds = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=SCOPES
        )
        service = build("drive", "v3", credentials=creds)
        logger.info("Autenticação no Google Drive realizada com sucesso.")
        return service
    except Exception as e:
        logger.error(f"Falha na autenticação com Google Drive: {e}")
        raise


def autenticar_azure_blob(
    connection_string: Optional[str] = None, sas_url: Optional[str] = None
) -> BlobServiceClient:
    """Autentica no Azure Blob Storage via Connection String ou Blob SAS URL."""
    try:
        if connection_string and connection_string.strip():
            client = BlobServiceClient.from_connection_string(connection_string.strip())
            logger.info("Autenticação no Azure Blob Storage via Connection String concluída.")
            return client
        if sas_url and sas_url.strip():
            client = BlobServiceClient(account_url=sas_url.strip())
            logger.info("Autenticação no Azure Blob Storage via SAS URL concluída.")
            return client
        raise ValueError("Informe uma Connection String ou uma Blob SAS URL válida.")
    except Exception as e:
        logger.error(f"Falha na autenticação com Azure Blob Storage: {e}")
        raise


def buscar_id_pasta_drive(drive_service, nome_pasta: str) -> str:
    """Localiza o ID da pasta específica no Google Drive pelo nome."""
    query = (
        f"name = '{nome_pasta}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    pastas = resultado.get("files", [])
    if not pastas:
        raise FileNotFoundError(
            f"Pasta '{nome_pasta}' não encontrada ou não compartilhada com a Service Account."
        )
    return pastas[0]["id"]


def listar_dados_origem(drive_service, nome_pasta: str = "Atividade_P2") -> List[Dict]:
    """
    Função obrigatória: Lista todos os arquivos da pasta de origem no Google Drive.
    """
    pasta_id = buscar_id_pasta_drive(drive_service, nome_pasta)
    query = (
        f"'{pasta_id}' in parents and "
        "mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    )
    resposta = (
        drive_service.files()
        .list(q=query, fields="files(id, name, mimeType, size, modifiedTime)")
        .execute()
    )
    arquivos = resposta.get("files", [])
    logger.info(f"[ORIGEM - Google Drive] {len(arquivos)} arquivo(s) encontrado(s) em '{nome_pasta}'.")
    for arq in arquivos:
        logger.info(f"  -> Origem: {arq['name']} (ID: {arq['id']} | Tipo: {arq['mimeType']})")
    return arquivos


def listar_dados_destino(
    blob_service_client: BlobServiceClient, container_name: str
) -> List[Dict]:
    """
    Função obrigatória: Lista todos os blobs presentes no contêiner de destino no Azure.
    """
    container_valido = normalizar_nome_container(container_name)
    container_client = blob_service_client.get_container_client(container_valido)

    if not container_client.exists():
        logger.warning(f"[DESTINO - Azure] Contêiner '{container_valido}' ainda não existe.")
        return []

    blobs = []
    for blob in container_client.list_blobs():
        blobs.append(
            {
                "name": blob.name,
                "size": blob.size,
                "last_modified": blob.last_modified.strftime("%Y-%m-%d %H:%M:%S")
                if blob.last_modified
                else "-",
            }
        )

    logger.info(f"[DESTINO - Azure] {len(blobs)} arquivo(s) no contêiner '{container_valido}'.")
    for b in blobs:
        logger.info(f"  -> Destino: {b['name']} ({b['size']} bytes)")
    return blobs


def _baixar_arquivo_drive(drive_service, arquivo: Dict) -> Tuple[str, bytes]:
    """Faz o download de arquivos binários ou exporta documentos nativos do Google."""
    file_id = arquivo["id"]
    nome_arquivo = arquivo["name"]
    mime_type = arquivo["mimeType"]

    buffer = io.BytesIO()

    if mime_type in GOOGLE_MIME_EXPORTS:
        export_mime, extensao = GOOGLE_MIME_EXPORTS[mime_type]
        request = drive_service.files().export_media(fileId=file_id, mimeType=export_mime)
        if not nome_arquivo.lower().endswith(extensao):
            nome_arquivo += extensao
    else:
        request = drive_service.files().get_media(fileId=file_id)

    downloader = MediaIoBaseDownload(buffer, request)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()

    buffer.seek(0)
    return nome_arquivo, buffer.read()


def migrar_arquivos(
    drive_service,
    blob_service_client: BlobServiceClient,
    nome_pasta_origem: str,
    container_destino: str,
) -> List[Dict]:
    """
    Copia todos os arquivos da pasta do Google Drive para o Azure Blob Storage
    e exibe no console o status (sucesso ou erro) de cada transferência.
    """
    container_valido = normalizar_nome_container(container_destino)
    container_client = blob_service_client.get_container_client(container_valido)

    try:
        container_client.create_container()
        logger.info(f"Contêiner '{container_valido}' criado no Azure Blob Storage.")
    except ResourceExistsError:
        logger.info(f"Contêiner '{container_valido}' já existente selecionado.")

    arquivos_origem = listar_dados_origem(drive_service, nome_pasta_origem)
    resultados = []

    print("\n" + "=" * 60)
    print(f"INICIANDO MIGRAÇÃO: '{nome_pasta_origem}' -> Azure ('{container_valido}')")
    print("=" * 60)

    for arquivo in arquivos_origem:
        nome_original = arquivo["name"]
        try:
            nome_final, conteudo_bytes = _baixar_arquivo_drive(drive_service, arquivo)
            blob_client = container_client.get_blob_client(nome_final)
            blob_client.upload_blob(conteudo_bytes, overwrite=True)

            msg_sucesso = f"[SUCESSO] '{nome_original}' -> '{nome_final}' ({len(conteudo_bytes)} bytes)"
            print(msg_sucesso)
            logger.info(msg_sucesso)

            resultados.append(
                {
                    "arquivo": nome_final,
                    "status": "Sucesso",
                    "tamanho_bytes": len(conteudo_bytes),
                    "detalhe": "Transferido com sucesso",
                }
            )
        except Exception as erro:
            msg_erro = f"[ERRO] Falha ao transferir '{nome_original}': {erro}"
            print(msg_erro)
            logger.error(msg_erro)

            resultados.append(
                {
                    "arquivo": nome_original,
                    "status": "Erro",
                    "tamanho_bytes": 0,
                    "detalhe": str(erro),
                }
            )

    print("=" * 60)
    print("MIGRAÇÃO FINALIZADA")
    print("=" * 60 + "\n")
    return resultados