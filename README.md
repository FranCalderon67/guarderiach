# PDF Extractor — Azure App Service

## Estructura
```
pdf-azure/
├── app.py              ← Servidor Flask (API + rutas)
├── requirements.txt
├── startup.sh          ← Comando de inicio para Azure
├── .env.example        ← Copiá como .env y completá
└── templates/
    ├── upload.html     ← Página para usuarios (/)
    └── admin.html      ← Panel admin (/admin)
```

## Configuración

1. Copiá `.env.example` como `.env`
2. Completá los valores:

```
AZURE_CONNECTION_STRING=   ← Azure Portal → Storage Account → Access keys
AZURE_CONTAINER_NAME=      ← Nombre del container (ej: documentos)
ADMIN_PASSWORD=            ← Contraseña del panel admin
```

## Correr en local

```bash
pip install -r requirements.txt
python app.py
```

- Página usuarios: http://localhost:8000
- Panel admin:     http://localhost:8000/admin

## Deploy en Azure App Service

1. Creá un App Service (Python 3.11, Linux)
2. En "Configuration → Application settings" agregá las variables de entorno:
   - AZURE_CONNECTION_STRING
   - AZURE_CONTAINER_NAME
   - ADMIN_PASSWORD
3. En "Configuration → General settings" → Startup Command:
   ```
   gunicorn --bind=0.0.0.0:8000 --timeout 120 app:app
   ```
4. Subí el código via GitHub Actions, ZIP deploy o Azure CLI:
   ```bash
   az webapp up --name TU-APP-NAME --resource-group TU-RG --runtime PYTHON:3.11
   ```

## Flujo

- **Usuarios** entran a `/` → escriben su nombre → suben PDFs → se guardan en Azure Blob
- **Admin** entra a `/admin` → ingresa contraseña → filtra por fecha → procesa PDFs → descarga .txt
