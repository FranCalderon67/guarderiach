from flask import Flask, request, jsonify, send_file, redirect, url_for, session
from flask_cors import CORS
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
import pdfplumber
import requests as http_requests
import re
import io
import os
import json
import uuid
from datetime import datetime, timezone
from functools import wraps

load_dotenv()

app = Flask(__name__, static_folder='static')
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
AZURE_CONNECTION_STRING = os.getenv('AZURE_CONNECTION_STRING', '')
AZURE_CONTAINER_NAME    = os.getenv('AZURE_CONTAINER_NAME', 'documentos')

# Ruta de almacenamiento: Azure Files montado como carpeta local en producción
# En desarrollo local usa la carpeta 'uploads/' del proyecto
STORAGE_PATH = '/capitalhumano' if os.path.exists('/capitalhumano') else os.path.join(os.path.dirname(__file__), 'uploads')

SAP_TOKEN_URL     = os.getenv('SAP_TOKEN_URL', 'https://distrocuyo-data.authentication.us10.hana.ondemand.com/oauth/token')
SAP_CLIENT_ID     = os.getenv('SAP_CLIENT_ID', '')
SAP_CLIENT_SECRET = os.getenv('SAP_CLIENT_SECRET', '')
SAP_API_URL       = 'https://distrocuyo-data.us10.hcs.cloud.sap/api/v1/datasphere/consumption/relational/HCM/DNI_personal/DNI_personal'

ENTRA_TENANT_ID      = os.getenv('ENTRA_TENANT_ID', '')
ENTRA_CLIENT_ID      = os.getenv('ENTRA_CLIENT_ID', '')
ENTRA_CLIENT_SECRET  = os.getenv('ENTRA_CLIENT_SECRET', '')
ENTRA_REDIRECT_URI   = os.getenv('ENTRA_REDIRECT_URI', 'http://localhost:8000/auth/callback')
ENTRA_ADMIN_GROUP_ID = os.getenv('ENTRA_ADMIN_GROUP_ID', '')

ENTRA_AUTHORITY     = f'https://login.microsoftonline.com/{ENTRA_TENANT_ID}'
ENTRA_SCOPES        = ['User.Read', 'GroupMember.Read.All']

def entra_configurado():
    return all([ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET])

# ── Auth helpers ──────────────────────────────────────────────────────────────
def get_msal_app():
    import msal
    return msal.ConfidentialClientApplication(
        ENTRA_CLIENT_ID,
        authority=ENTRA_AUTHORITY,
        client_credential=ENTRA_CLIENT_SECRET
    )

def get_user_groups(access_token):
    res = http_requests.get(
        'https://graph.microsoft.com/v1.0/me/memberOf',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=10
    )
    if res.status_code != 200:
        return []
    data = res.json()
    return [g.get('id', '') for g in data.get('value', [])]

def is_admin_user():
    if not session.get('user'):
        return False
    if not ENTRA_ADMIN_GROUP_ID:
        return session['user'].get('role') == 'admin'
    return ENTRA_ADMIN_GROUP_ID in session.get('groups', [])

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login_page'))
        if not is_admin_user():
            return redirect(url_for('upload_page'))
        return f(*args, **kwargs)
    return decorated

# ── SAP OAuth2 ────────────────────────────────────────────────────────────────
_sap_token_cache = {'token': None, 'expires_at': 0}

def get_sap_token():
    import time
    now = time.time()
    if _sap_token_cache['token'] and now < _sap_token_cache['expires_at'] - 60:
        return _sap_token_cache['token']
    if not SAP_CLIENT_ID or not SAP_CLIENT_SECRET:
        raise Exception('SAP no configurado.')
    res = http_requests.post(
        SAP_TOKEN_URL,
        data={'grant_type': 'client_credentials', 'client_id': SAP_CLIENT_ID, 'client_secret': SAP_CLIENT_SECRET},
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
    res   = http_requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=10)
    res.raise_for_status()
    data  = res.json()
    value = data.get('value', [])
    if not value:
        return None
    legajo = value[0].get('Numero_de_personal', '')
    return str(int(legajo)) if legajo.isdigit() else legajo

# ── Azure Blob ────────────────────────────────────────────────────────────────
def azure_configurado():
    # Con Azure Files montado como carpeta local, siempre usamos filesystem
    return False

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
            name=blob_name, data=file_bytes, metadata=metadata,
            content_settings=ContentSettings(content_type='application/pdf'),
            overwrite=True
        )
    else:
        os.makedirs(STORAGE_PATH, exist_ok=True)
        safe_path = os.path.join(STORAGE_PATH, blob_name.replace('/', '_'))
        with open(safe_path, 'wb') as f:
            f.write(file_bytes)
        with open(safe_path + '.meta.json', 'w', encoding='utf-8') as f:
            json.dump({
                'uploader': uploader_name, 'dni': dni,
                'original_name': original_name,
                'uploaded_at': datetime.now(timezone.utc).isoformat(),
                'blob_name': blob_name, 'size': len(file_bytes)
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
        if not os.path.exists(STORAGE_PATH):
            return []
        for fname in os.listdir(STORAGE_PATH):
            if not fname.endswith('.meta.json'):
                continue
            with open(os.path.join(STORAGE_PATH, fname), encoding='utf-8') as f:
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
        return container.get_blob_client(blob_name).download_blob().readall()
    else:
        safe_path = os.path.join(STORAGE_PATH, blob_name.replace('/', '_'))
        with open(safe_path, 'rb') as f:
            return f.read()

# ── PDF parsing ───────────────────────────────────────────────────────────────
def extract_text_from_bytes(file_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"
    return text

def find_field(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return None

def detect_type(text):
    if re.search(r'RECIBO DE SUELDO', text, re.IGNORECASE): return 'recibo'
    if re.search(r'RECIBIMOS DE:', text, re.IGNORECASE): return 'recibo_colegio'
    if re.search(r'Nombre:.*?Apellido:', text, re.IGNORECASE | re.DOTALL): return 'factura_apdes'
    if re.search(r'Apellido y Nombre\s*/\s*Raz', text, re.IGNORECASE): return 'factura_arca'
    return 'desconocido'

def parse_nombre(text, doc_type):
    if doc_type == 'recibo':
        r = find_field(text, [r'Datos del Empleador\s*\nApellido y Nombre:\s*([A-ZAÉÍÓÚÜÑ\s]+?)\s+CUIL'])
        return r.strip() if r else "No encontrado"
    if doc_type == 'factura_arca':
        r = find_field(text, [r'Apellido y Nombre\s*/\s*Raz[oó]n Social:\s*([A-Za-záéíóúüñÁÉÍÓÚÜÑ\s]+?)(?:\n|Domicilio|$)'])
        return r.strip() if r else "No encontrado"
    if doc_type == 'factura_apdes':
        nombre   = find_field(text, [r'Nombre:\s*([A-Za-záéíóúüñÁÉÍÓÚÜÑ]+(?:\s+[A-Za-záéíóúüñÁÉÍÓÚÜÑ]+)*?)\s+Apellido:'])
        apellido = find_field(text, [r'Apellido:\s*([A-Za-záéíóúüñÁÉÍÓÚÜÑ]+(?:\s+[A-Za-záéíóúüñÁÉÍÓÚÜÑ]+)*?)(?:\n|Familia|Barrio|$)'])
        if nombre and apellido:
            return f"{apellido.strip()} {nombre.strip()}"
        return nombre or apellido or "No encontrado"
    if doc_type == 'recibo_colegio':
        r = find_field(text, [r'RECIBIMOS DE:\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s]+?)(?:\n|DIRECCIÓN|Nro)'])
        return r.strip() if r else "No encontrado"
    return "No encontrado"

def parse_importe(text, doc_type):
    raw = None
    if doc_type == 'factura_arca':
        raw = find_field(text, [r'Importe Total:\s*\$?\s*([\d.,]+)'])
    elif doc_type in ('factura_apdes', 'recibo_colegio'):
        raw = find_field(text, [r'TOTAL\s*\$\s*([\d.,]+)'])
    elif doc_type == 'recibo':
        m = re.search(r'(?:^|\n)Total\s*\$\s*([\d.,]+)', text, re.MULTILINE)
        if m: raw = m.group(1)
    if not raw:
        return "No encontrado"
    try:
        raw = raw.strip()
        if ',' in raw and '.' in raw:
            raw = raw.replace('.', '').replace(',', '.') if raw.index('.') < raw.index(',') else raw.replace(',', '')
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
# RUTAS DE AUTENTICACIÓN
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/login')
def login_page():
    # Si ya está logueado, redirigir según rol
    if session.get('user'):
        return redirect(url_for('admin_page') if is_admin_user() else url_for('upload_page'))
    with open(os.path.join(os.path.dirname(__file__), 'templates', 'login.html'), encoding='utf-8') as f:
        return f.read()

@app.route('/auth/login')
def auth_login():
    if not entra_configurado():
        return jsonify({'error': 'Entra ID no configurado. Completá el .env'}), 500
    msal_app = get_msal_app()
    state    = str(uuid.uuid4())
    session['auth_state'] = state
    auth_url = msal_app.get_authorization_request_url(
        scopes=ENTRA_SCOPES,
        state=state,
        redirect_uri=ENTRA_REDIRECT_URI
    )
    return redirect(auth_url)

@app.route('/auth/callback')
def auth_callback():
    if request.args.get('state') != session.get('auth_state'):
        return redirect(url_for('login_page'))

    code = request.args.get('code')
    if not code:
        return redirect(url_for('login_page'))

    msal_app = get_msal_app()
    result   = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=ENTRA_SCOPES,
        redirect_uri=ENTRA_REDIRECT_URI
    )

    if 'error' in result:
        return redirect(url_for('login_page'))

    # Obtener info del usuario
    access_token = result.get('access_token')
    user_info    = http_requests.get(
        'https://graph.microsoft.com/v1.0/me',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=10
    ).json()

    # Obtener grupos para determinar rol
    groups = get_user_groups(access_token)

    session['user'] = {
        'name':  user_info.get('displayName', ''),
        'email': user_info.get('mail') or user_info.get('userPrincipalName', ''),
        'id':    user_info.get('id', '')
    }
    session['groups']       = groups
    session['access_token'] = access_token

    # Redirigir según rol
    if is_admin_user():
        return redirect(url_for('admin_page'))
    return redirect(url_for('upload_page'))

@app.route('/auth/logout')
def auth_logout():
    session.clear()
    if entra_configurado():
        logout_url = f"{ENTRA_AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('login_page', _external=True)}"
        return redirect(logout_url)
    return redirect(url_for('login_page'))

# ══════════════════════════════════════════════════════════════════════════════
# RUTAS PRINCIPALES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if session.get('user'):
        return redirect(url_for('admin_page') if is_admin_user() else url_for('upload_page'))
    return redirect(url_for('login_page'))

@app.route('/upload')
@login_required
def upload_page():
    with open(os.path.join(os.path.dirname(__file__), 'templates', 'upload.html'), encoding='utf-8') as f:
        content = f.read()
    user = session.get('user', {})
    content = content.replace('{{USER_NAME}}', user.get('name', ''))
    return content

@app.route('/admin')
@admin_required
def admin_page():
    with open(os.path.join(os.path.dirname(__file__), 'templates', 'admin.html'), encoding='utf-8') as f:
        content = f.read()
    user = session.get('user', {})
    content = content.replace('{{USER_NAME}}', user.get('name', ''))
    return content

# ══════════════════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/me')
@login_required
def api_me():
    user = session.get('user', {})
    return jsonify({
        'name':     user.get('name', ''),
        'email':    user.get('email', ''),
        'is_admin': is_admin_user()
    })

@app.route('/api/upload', methods=['POST'])
@login_required
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
@admin_required
def api_listar():
    date_from = request.json.get('date_from', '')
    date_to   = request.json.get('date_to', '')
    try:
        df       = datetime.strptime(date_from, '%Y-%m-%d').date()
        dt       = datetime.strptime(date_to,   '%Y-%m-%d').date()
        archivos = list_blobs_by_date(df, dt)
        return jsonify({'archivos': archivos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/preview', methods=['POST'])
@admin_required
def api_preview():
    blob_names = request.json.get('blob_names', [])
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

@app.route('/api/admin/buscar-legajos-bulk', methods=['POST'])
@admin_required
def api_buscar_legajos_bulk():
    items      = request.json.get('items', [])
    resultados = {}
    for item in items:
        dni       = item.get('dni', '')
        blob_name = item.get('blob_name', '')
        try:
            resultados[blob_name] = buscar_legajo_por_dni(dni) if dni else None
        except Exception:
            resultados[blob_name] = None
    return jsonify({'legajos': resultados})

@app.route('/api/admin/generar-txt', methods=['POST'])
@admin_required
def api_generar_txt():
    campos            = request.json.get('campos', [])
    importes_override = request.json.get('importes_override', {})
    filas             = request.json.get('filas', [])

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