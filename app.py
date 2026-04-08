from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
import pdfplumber
import requests as http_requests
import re
import io
import os
import json
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)
CORS(app)

AZURE_CONNECTION_STRING = os.getenv('AZURE_CONNECTION_STRING', '')
AZURE_CONTAINER_NAME    = os.getenv('AZURE_CONTAINER_NAME', 'documentos')
ADMIN_PASSWORD          = os.getenv('ADMIN_PASSWORD', 'admin123')
UPLOADS_LOCAL           = os.path.join(os.path.dirname(__file__), 'uploads')

SAP_TOKEN_URL  = os.getenv('SAP_TOKEN_URL',  '')
SAP_CLIENT_ID  = os.getenv('SAP_CLIENT_ID',  '')
SAP_CLIENT_SECRET = os.getenv('SAP_CLIENT_SECRET', '')
SAP_API_URL    = 'https://distrocuyo-data.us10.hcs.cloud.sap/api/v1/datasphere/consumption/relational/HCM/DNI_personal/DNI_personal'

# ── SAP OAuth2 ────────────────────────────────────────────────────────────────
_sap_token_cache = {'token': None, 'expires_at': 0}

def get_sap_token():
    import time
    now = time.time()
    if _sap_token_cache['token'] and now < _sap_token_cache['expires_at'] - 60:
        return _sap_token_cache['token']

    if not SAP_CLIENT_ID or not SAP_CLIENT_SECRET:
        raise Exception('SAP no configurado. Completá SAP_CLIENT_ID y SAP_CLIENT_SECRET en el .env')

    res = http_requests.post(
        SAP_TOKEN_URL,
        data={
            'grant_type':    'client_credentials',
            'client_id':     SAP_CLIENT_ID,
            'client_secret': SAP_CLIENT_SECRET,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=10
    )
    res.raise_for_status()
    data = res.json()
    _sap_token_cache['token']      = data['access_token']
    _sap_token_cache['expires_at'] = now + data.get('expires_in', 3600)
    return _sap_token_cache['token']

def buscar_legajo_por_dni(dni):
    token = get_sap_token()
    url   = f"{SAP_API_URL}?$filter=DNI eq '{dni}'&$top=1"
    res   = http_requests.get(
        url,
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    res.raise_for_status()
    data  = res.json()
    value = data.get('value', [])
    if not value:
        return None
    legajo = value[0].get('Numero_de_personal', '')
    return str(int(legajo)) if legajo.isdigit() else legajo

    token = get_sap_token()
    res   = http_requests.get(
        SAP_API_URL,
        headers={'Authorization': f'Bearer {token}'},
        params={'$filter': f"DNI eq '{dni}'", '$top': '1'},
        timeout=10
    )
    res.raise_for_status()
    data  = res.json()
    value = data.get('value', [])
    if not value:
        return None
    # Numero_de_personal viene como "00000007" — sacamos los ceros a la izquierda
    legajo = value[0].get('Numero_de_personal', '')
    return str(int(legajo)) if legajo.isdigit() else legajo

# ── Azure helpers ─────────────────────────────────────────────────────────────
def azure_configurado():
    return AZURE_CONNECTION_STRING and 'TU_ACCOUNT' not in AZURE_CONNECTION_STRING

def get_container_client():
    client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    return client.get_container_client(AZURE_CONTAINER_NAME)

def upload_to_azure(file_bytes, blob_name, original_name, uploader_name, dni):
    if azure_configurado():
        container = get_container_client()
        metadata  = {
            'uploader':      uploader_name,
            'dni':           dni,
            'original_name': original_name,
            'uploaded_at':   datetime.now(timezone.utc).isoformat()
        }
        container.upload_blob(
            name=blob_name,
            data=file_bytes,
            metadata=metadata,
            content_settings=ContentSettings(content_type='application/pdf'),
            overwrite=True
        )
    else:
        os.makedirs(UPLOADS_LOCAL, exist_ok=True)
        safe_path = os.path.join(UPLOADS_LOCAL, blob_name.replace('/', '_'))
        with open(safe_path, 'wb') as f:
            f.write(file_bytes)
        meta_path = safe_path + '.meta.json'
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({
                'uploader':      uploader_name,
                'dni':           dni,
                'original_name': original_name,
                'uploaded_at':   datetime.now(timezone.utc).isoformat(),
                'blob_name':     blob_name,
                'size':          len(file_bytes)
            }, f, ensure_ascii=False)

def list_blobs_by_date(date_from, date_to):
    if azure_configurado():
        container  = get_container_client()
        resultados = []
        for blob in container.list_blobs(include=['metadata']):
            meta        = blob.metadata or {}
            uploaded_at = meta.get('uploaded_at', '')
            if uploaded_at:
                try:
                    dt       = datetime.fromisoformat(uploaded_at.replace('Z', '+00:00'))
                    dt_local = dt.replace(tzinfo=None)
                    if date_from <= dt_local.date() <= date_to:
                        resultados.append({
                            'blob_name':     blob.name,
                            'original_name': meta.get('original_name', blob.name),
                            'uploader':      meta.get('uploader', 'Desconocido'),
                            'dni':           meta.get('dni', ''),
                            'uploaded_at':   dt_local.strftime('%d/%m/%Y %H:%M'),
                            'size_kb':       round(blob.size / 1024, 1)
                        })
                except:
                    pass
        return sorted(resultados, key=lambda x: x['uploaded_at'], reverse=True)
    else:
        resultados = []
        if not os.path.exists(UPLOADS_LOCAL):
            return []
        for fname in os.listdir(UPLOADS_LOCAL):
            if not fname.endswith('.meta.json'):
                continue
            meta_path = os.path.join(UPLOADS_LOCAL, fname)
            with open(meta_path, encoding='utf-8') as f:
                meta = json.load(f)
            uploaded_at = meta.get('uploaded_at', '')
            try:
                dt       = datetime.fromisoformat(uploaded_at.replace('Z', '+00:00'))
                dt_local = dt.replace(tzinfo=None)
                if date_from <= dt_local.date() <= date_to:
                    resultados.append({
                        'blob_name':     meta.get('blob_name', fname),
                        'original_name': meta.get('original_name', fname),
                        'uploader':      meta.get('uploader', 'Desconocido'),
                        'dni':           meta.get('dni', ''),
                        'uploaded_at':   dt_local.strftime('%d/%m/%Y %H:%M'),
                        'size_kb':       round(meta.get('size', 0) / 1024, 1)
                    })
            except:
                pass
        return sorted(resultados, key=lambda x: x['uploaded_at'], reverse=True)

def download_blob(blob_name):
    if azure_configurado():
        container = get_container_client()
        blob      = container.get_blob_client(blob_name)
        return blob.download_blob().readall()
    else:
        safe_path = os.path.join(UPLOADS_LOCAL, blob_name.replace('/', '_'))
        with open(safe_path, 'rb') as f:
            return f.read()

# ── PDF parsing ───────────────────────────────────────────────────────────────
def extract_text_from_bytes(file_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text

def find_field(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return None

def detect_type(text):
    if re.search(r'RECIBO DE SUELDO', text, re.IGNORECASE):
        return 'recibo'
    if re.search(r'RECIBIMOS DE:', text, re.IGNORECASE):
        return 'recibo_colegio'
    if re.search(r'Nombre:.*?Apellido:', text, re.IGNORECASE | re.DOTALL):
        return 'factura_apdes'
    if re.search(r'Apellido y Nombre\s*/\s*Raz', text, re.IGNORECASE):
        return 'factura_arca'
    return 'desconocido'

def parse_nombre(text, doc_type):
    if doc_type == 'recibo':
        result = find_field(text, [r'Datos del Empleador\s*\nApellido y Nombre:\s*([A-ZAÉÍÓÚÜÑ\s]+?)\s+CUIL'])
        return result.strip() if result else "No encontrado"
    if doc_type == 'factura_arca':
        result = find_field(text, [r'Apellido y Nombre\s*/\s*Raz[oó]n Social:\s*([A-Za-záéíóúüñÁÉÍÓÚÜÑ\s]+?)(?:\n|Domicilio|$)'])
        return result.strip() if result else "No encontrado"
    if doc_type == 'factura_apdes':
        nombre   = find_field(text, [r'Nombre:\s*([A-Za-záéíóúüñÁÉÍÓÚÜÑ]+(?:\s+[A-Za-záéíóúüñÁÉÍÓÚÜÑ]+)*?)\s+Apellido:'])
        apellido = find_field(text, [r'Apellido:\s*([A-Za-záéíóúüñÁÉÍÓÚÜÑ]+(?:\s+[A-Za-záéíóúüñÁÉÍÓÚÜÑ]+)*?)(?:\n|Familia|Barrio|$)'])
        if nombre and apellido:
            return f"{apellido.strip()} {nombre.strip()}"
        return nombre or apellido or "No encontrado"
    if doc_type == 'recibo_colegio':
        result = find_field(text, [r'RECIBIMOS DE:\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s]+?)(?:\n|DIRECCIÓN|Nro)'])
        return result.strip() if result else "No encontrado"
    return "No encontrado"

def parse_importe(text, doc_type):
    raw = None
    if doc_type == 'factura_arca':
        raw = find_field(text, [r'Importe Total:\s*\$?\s*([\d.,]+)'])
    elif doc_type in ('factura_apdes', 'recibo_colegio'):
        raw = find_field(text, [r'TOTAL\s*\$\s*([\d.,]+)'])
    elif doc_type == 'recibo':
        match = re.search(r'(?:^|\n)Total\s*\$\s*([\d.,]+)', text, re.MULTILINE)
        if match:
            raw = match.group(1)
    if not raw:
        return "No encontrado"
    try:
        raw = raw.strip()
        if ',' in raw and '.' in raw:
            if raw.index('.') < raw.index(','):
                raw = raw.replace('.', '').replace(',', '.')
            else:
                raw = raw.replace(',', '')
        elif ',' in raw:
            raw = raw.replace(',', '.')
        numero = float(raw)
        return f"$ {numero:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    except:
        return f"$ {raw}"

def parse_document(text):
    partes = re.split(r'\n(?:ORIGINAL|DUPLICADO|TRIPLICADO)\n', text)
    texto_principal = partes[0] if partes else text
    doc_type = detect_type(texto_principal)
    if doc_type == 'desconocido':
        doc_type = detect_type(text)
        texto_principal = text
    return {
        "nombre":  parse_nombre(texto_principal, doc_type),
        "importe": parse_importe(texto_principal, doc_type),
        "tipo":    doc_type
    }

# ══════════════════════════════════════════════════════════════════════════════
# RUTAS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    with open(os.path.join(os.path.dirname(__file__), 'templates', 'upload.html'), encoding='utf-8') as f:
        return f.read()

@app.route('/admin')
def admin():
    with open(os.path.join(os.path.dirname(__file__), 'templates', 'admin.html'), encoding='utf-8') as f:
        return f.read()

@app.route('/api/upload', methods=['POST'])
def api_upload():
    nombre = request.form.get('nombre', '').strip()
    dni    = request.form.get('dni', '').strip()
    files  = request.files.getlist('pdfs')

    if not nombre:
        return jsonify({'error': 'El nombre es obligatorio.'}), 400
    if not dni:
        return jsonify({'error': 'El DNI es obligatorio.'}), 400
    if not files:
        return jsonify({'error': 'Seleccioná al menos un PDF.'}), 400

    subidos = []
    errores = []

    for file in files:
        if not file.filename.endswith('.pdf'):
            errores.append(f"{file.filename}: no es un PDF.")
            continue
        try:
            file_bytes  = file.read()
            timestamp   = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            safe_nombre = re.sub(r'[^a-zA-Z0-9_-]', '_', nombre)
            blob_name   = f"{safe_nombre}/{timestamp}_{file.filename}"
            upload_to_azure(file_bytes, blob_name, file.filename, nombre, dni)
            subidos.append(file.filename)
        except Exception as e:
            errores.append(f"{file.filename}: {str(e)}")

    if not subidos:
        return jsonify({'error': 'No se pudo subir ningún archivo. ' + ' '.join(errores)}), 500

    return jsonify({'subidos': subidos, 'errores': errores})

@app.route('/api/admin/listar', methods=['POST'])
def api_listar():
    password  = request.json.get('password', '')
    date_from = request.json.get('date_from', '')
    date_to   = request.json.get('date_to', '')

    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Contraseña incorrecta.'}), 401

    try:
        df       = datetime.strptime(date_from, '%Y-%m-%d').date()
        dt       = datetime.strptime(date_to,   '%Y-%m-%d').date()
        archivos = list_blobs_by_date(df, dt)
        return jsonify({'archivos': archivos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/preview', methods=['POST'])
def api_preview():
    password   = request.json.get('password', '')
    blob_names = request.json.get('blob_names', [])

    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Contraseña incorrecta.'}), 401

    resultados = []
    for blob_name in blob_names:
        try:
            file_bytes = download_blob(blob_name)
            text       = extract_text_from_bytes(file_bytes)
            data       = parse_document(text)
            resultados.append({'blob_name': blob_name, 'archivo': blob_name.split('/')[-1], **data})
        except Exception as e:
            resultados.append({'blob_name': blob_name, 'archivo': blob_name.split('/')[-1], 'nombre': 'Error', 'importe': str(e), 'tipo': 'error'})

    return jsonify(resultados)

# ── Nueva ruta: buscar legajo en SAP por DNI ──────────────────────────────────
@app.route('/api/admin/buscar-legajo', methods=['POST'])
def api_buscar_legajo():
    password = request.json.get('password', '')
    dni      = request.json.get('dni', '')

    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Contraseña incorrecta.'}), 401
    if not dni:
        return jsonify({'error': 'DNI requerido.'}), 400

    try:
        legajo = buscar_legajo_por_dni(dni)
        if legajo:
            return jsonify({'legajo': legajo})
        else:
            return jsonify({'legajo': None, 'mensaje': 'DNI no encontrado en SAP'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Nueva ruta: buscar legajos para todos los archivos del preview ─────────────
@app.route('/api/admin/buscar-legajos-bulk', methods=['POST'])
def api_buscar_legajos_bulk():
    password = request.json.get('password', '')
    items    = request.json.get('items', [])  # [{ blob_name, dni }, ...]

    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Contraseña incorrecta.'}), 401

    resultados = {}
    for item in items:
        dni       = item.get('dni', '')
        blob_name = item.get('blob_name', '')
        if not dni:
            resultados[blob_name] = None
            continue
        try:
            legajo = buscar_legajo_por_dni(dni)
            resultados[blob_name] = legajo
        except Exception as e:
            resultados[blob_name] = None

    return jsonify({'legajos': resultados})

@app.route('/api/admin/generar-txt', methods=['POST'])
def api_generar_txt():
    password          = request.json.get('password', '')
    campos            = request.json.get('campos', [])
    importes_override = request.json.get('importes_override', {})
    filas             = request.json.get('filas', [])

    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Contraseña incorrecta.'}), 401

    lines = []
    lines.append("Legajo SAP\tCC-N\t Importe \tCantidad\tFecha\t\t")

    for fila in filas:
        legajo = fila.get('legajo', '')
        fecha  = fila.get('fecha', '')
        if 'importe' in campos:
            imp = importes_override.get(fila.get('blob_name', ''), fila.get('importe', ''))
            imp = imp.replace('$ ', '').replace('$', '').strip()
        else:
            imp = ''
        lines.append(f"{legajo}\t2412\t {imp} \t\t{fecha}\t\t")

    for _ in range(40):
        lines.append("\t\t\t\t\t\t")

    buffer = io.BytesIO("\n".join(lines).encode('utf-8'))
    buffer.seek(0)
    return send_file(buffer, mimetype='text/plain', as_attachment=True, download_name='reporte.txt')

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    print(f"Servidor corriendo en http://localhost:{port}")
    app.run(debug=True, port=port)
